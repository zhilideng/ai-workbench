"""从看板发送新任务，按需拆分并调度独立会话。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from graphlib import CycleError, TopologicalSorter

from workbench.core.errors import BadRequest, Conflict, NotFound, WorkbenchError
from workbench.db.repositories import ParallelGroupRepo, TaskRepo
from workbench.engines import get_engine
from workbench.engines.planning import plan_with_cli
from workbench.services import connections, tasks
from workbench.services.runner import Runner

log = logging.getLogger(__name__)

ONE = re.compile(r"(?:只(?:在|用|开)?|仅(?:在|用|开)?|放(?:在|进)|合并到|都在|统一用|集中在|(?<!分别)(?<!各自)(?<!各)在)"
                 r"\s*(?:同一个|一个|1\s*个|单个)\s*(?:会话|对话)"
                 r"|(?:在|用|开)\s*同一(?:个)?\s*(?:会话|对话)|(?:只用|放进|放在|只开)\s*单(?:会话|对话)"
                 r"|(?:^|[，。！？；：\n]|请|先|就|统一|都)\s*(?:开|建|创建|使用|用|启动)\s*(?:一个|1\s*个|单个)\s*(?:会话|对话)"
                 r"|(?:不要|不用|无需|不必|别|禁止)\s*(?:拆分|分开(?:执行|处理|完成|做)|并行(?:执行|处理|完成|做)|"
                 r"(?:分别|各自)\s*(?:开|建|创建|使用)\s*(?:会话|对话)|"
                 r"(?:再)?(?:开|建|使用|启动)\s*多(?:个|条)?\s*(?:会话|对话))"
                 r"|\b(?:single|one) (?:conversation|session) for (?:all|everything)\b", re.I)
MULTI = re.compile(
    r"(?:分别|各自|各|每(?:个|项)[^。！？\n]{0,18})(?:开|建|创建|分配|对应|用|安排|使用|进入|在)"
    r"\s*(?:一|1)?\s*个?\s*(?:单独的?|独立的?|不同的?)?\s*(?:会话|对话)"
    r"|各(?:自)?一个(?:会话|对话)"
    r"|(?:开|建|创建|使用|用|开启|启动|发起)\s*(?:多(?:个|条)?|[2-9][0-9]*\s*个|[1-9][0-9]+\s*个|"
    r"(?:十[一二三四五六七八九]?|[两二三四五六七八九]十?[一二三四五六七八九]?)\s*个|独立的?|不同的?)"
    r"\s*(?:独立的?|不同的?|单独的?)?\s*(?:会话|对话)"
    r"|(?:分成|拆成|拆为)\s*[^。，。！？\n]{0,10}(?:会话|对话)"
    r"|(?:^|[，。！？；：\n]|请|需要|同时|一起)\s*(?:把[^，。！？；\n]{0,24})?\s*(?:并行|同时|分别|分开)\s*(?:执行|完成|处理|去做|做)"
    r"|(?:任务|子任务|事项|会话|对话)[^。！？\n]{0,20}(?:去)?(?:并行|分别)\s*(?:执行|完成|处理|去做)"
    r"|\b(?:separate|independent|parallel) (?:sessions|conversations)\b"
    r"|\b(?:run|execute|complete)\b[^.\n]{0,45}\b(?:tasks|items) in parallel\b", re.I)
FEATURE_SUFFIX = re.compile(r"\s*(?:的)?\s*(?:按钮|功能|入口|选项|机制|界面|流程|接口|示例|交互|文案|逻辑|测试|场景|用例)")


def parallel_intent(prompt: str) -> bool:
    """只识别明确的派发措辞，常规提示词直接送给现有任务。"""
    # 粘贴整段任务时，输入法可能把外层三个单引号转换为混用的弯引号。
    text = re.sub(r"\A['‘’]{3}([^\n]*\n[\s\S]*)['‘’]{3}\Z", r"\1", prompt.strip())
    text = re.sub(r"```[\s\S]*?```|`[^`\n]+`|「[^」\n]*」|『[^』\n]*』|“[^”\n]*”", "", text)
    return not ONE.search(text) and any(not FEATURE_SUFFIX.match(text, match.end()) for match in MULTI.finditer(text))


async def make_plan(settings: dict, prompt: str) -> dict:
    return await plan_with_cli(settings, prompt, connections.of_task(settings))


def _get(group_id: int) -> dict:
    group = ParallelGroupRepo.get(group_id)
    if not group:
        raise NotFound("任务组不存在")
    return group


def _public(group: dict) -> dict:
    return {key: group[key] for key in ("id", "title", "project", "state", "prompt", "error", "updated_at")}


def list_groups() -> list[dict]:
    return [_public(group) for group in ParallelGroupRepo.list()]


async def wait_for_group(runner: Runner, group_id: int) -> dict:
    plan = runner.planning.get(group_id)
    if plan is not None:
        await asyncio.wait({plan}, timeout=25)
    return _public(_get(group_id))


def _response(group: dict) -> dict:
    if group["state"] == "single":
        return {"task": TaskRepo.get(group["single_task_id"]), "group": None}
    return {"task": None, "group": _public(group)}


def _plan_members(plan: dict, fields: dict) -> tuple[str, list[dict], list[list[int]]]:
    title, entries = plan.get("title"), plan.get("tasks")
    if not isinstance(entries, list) or not entries:
        reason = plan.get("blocked_reason")
        raise BadRequest(reason.strip()[:500] if isinstance(reason, str) and reason.strip() else "CLI 没有返回可执行的任务")
    if not isinstance(title, str) or not title.strip():
        title = fields["title"]
    members = []
    dependencies: dict[str, list[str]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise BadRequest("CLI 返回了无效的子任务")
        key = entry.get("id", str(index + 1))
        prerequisites = entry.get("depends_on", [])
        if not isinstance(key, str) or not key.strip() or key in dependencies:
            raise BadRequest("CLI 返回了无效或重复的会话编号")
        if not isinstance(prerequisites, list) or any(not isinstance(item, str) for item in prerequisites):
            raise BadRequest("CLI 返回了无效的前置任务")
        dependencies[key] = list(dict.fromkeys(prerequisites))
        name, prompt, engine = entry.get("title"), entry.get("prompt"), entry.get("engine")
        if not isinstance(prompt, str) or not prompt.strip():
            raise BadRequest("CLI 返回的会话缺少执行内容")
        if not isinstance(name, str) or not name.strip():
            name = prompt
        member = {**fields, "title": tasks.derive_title(name), "description": prompt.strip(), "use_worktree": 0}
        if engine and engine != fields["engine"]:
            if not isinstance(engine, str):
                raise BadRequest("CLI 返回了无效的引擎")
            selected = get_engine(engine)
            if not fields["ssh_id"] and not selected.available():
                raise BadRequest(f"找不到子会话使用的 {selected.label} CLI")
            member.update(engine=engine, model="", effort="")
        members.append(member)
    positions = {key: index for index, key in enumerate(dependencies)}
    if any(key not in positions for prerequisites in dependencies.values() for key in prerequisites):
        raise BadRequest("CLI 引用了不存在的前置任务")
    try:
        TopologicalSorter(dependencies).prepare()
    except CycleError as e:
        raise BadRequest("CLI 返回了循环依赖，请重试拆分") from e
    return tasks.derive_title(title), members, [[positions[key] for key in keys] for keys in dependencies.values()]


async def _start_member(runner: Runner, task_id: int) -> None:
    try:
        await runner.start(task_id)
    except Exception as e:  # noqa: BLE001
        log.warning("无法启动任务 %s (%s)", task_id, type(e).__name__)
        TaskRepo.update(task_id, status="failed")


async def _split(runner: Runner, group_id: int, fields: dict) -> None:
    try:
        async with runner.planning_sem:
            plan = await make_plan(fields, fields["description"])
        title, members, dependencies = _plan_members(plan, fields)
        ParallelGroupRepo.create_members(group_id, title, members, dependencies)
    except asyncio.CancelledError:
        ParallelGroupRepo.update(group_id, state="failed", error="拆分中断，请重试")
        raise
    except TimeoutError:
        ParallelGroupRepo.update(group_id, state="failed", error="拆分超时，请重试")
    except Exception as e:  # noqa: BLE001
        log.warning("任务组 %s 拆分失败 (%s)", group_id, type(e).__name__)
        ParallelGroupRepo.update(group_id, state="failed",
                                 error=e.message if isinstance(e, WorkbenchError) else "CLI 拆分失败，请检查引擎或连接后重试")
    else:
        await runner.start_ready(group_id)
    finally:
        runner.planning.pop(group_id, None)


async def send(runner: Runner, request_id: str, **request) -> dict:
    run_now = request.pop("run_now")
    if not run_now or not parallel_intent(request["prompt"]):
        return {"task": await tasks.create_task(runner, run_now=run_now, **request), "group": None}
    request_id = request_id or uuid.uuid4().hex
    existing = ParallelGroupRepo.find(request_id)
    if existing:
        return _response(existing)
    fields = tasks.prepare_task(**request)
    group_id = ParallelGroupRepo.create(request_id, fields)
    runner.planning[group_id] = asyncio.create_task(_split(runner, group_id, fields))
    return _response(_get(group_id))


def retry(runner: Runner, group_id: int) -> dict:
    group = _get(group_id)
    if group["state"] == "planning":
        return _response(group)
    if group["state"] != "failed":
        raise Conflict("只有拆分失败的任务组可以重试")
    fields = json.loads(group["task_fields"])
    ParallelGroupRepo.update(group_id, state="planning", error="")
    runner.planning[group_id] = asyncio.create_task(_split(runner, group_id, fields))
    return _response(_get(group_id))


async def use_single(runner: Runner, group_id: int) -> dict:
    group = _get(group_id)
    if group["state"] == "single":
        return _response(group)
    if group["state"] != "failed":
        raise Conflict("只有拆分失败的任务组可以改用单会话")
    task_id = ParallelGroupRepo.create_single(group_id, json.loads(group["task_fields"]))
    await _start_member(runner, task_id)
    return _response(_get(group_id))
