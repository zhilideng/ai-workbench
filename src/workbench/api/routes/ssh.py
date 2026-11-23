from fastapi import APIRouter, Depends

from workbench.api.deps import get_runner
from workbench.api.schemas import SshIn
from workbench.services import connections
from workbench.services.projects import project_sources
from workbench.services.runner import Runner

router = APIRouter(tags=["ssh"])


@router.get("/ssh/config-hosts")
def config_hosts():
    return connections.config_hosts()


@router.get("/ssh/folder-connections")
def list_folder_connections():
    return connections.list_for("")


@router.post("/ssh/folder-connections", status_code=201)
def create_folder_connection(body: SshIn):
    return connections.create("", body.model_dump())


@router.put("/ssh/folder-connections/{cid}")
def update_folder_connection(cid: int, body: SshIn):
    return connections.update("", cid, body.model_dump())


@router.post("/ssh/folder-connections/{cid}/connect")
async def connect_folder_connection(cid: int):
    return await connections.connect("", cid)


@router.post("/ssh/folder-connections/{cid}/disconnect")
async def disconnect_folder_connection(cid: int, runner: Runner = Depends(get_runner)):
    return await connections.disconnect("", cid, runner)


@router.get("/projects/{name}/ssh")
def list_ssh(name: str):
    project_sources(name)
    return connections.list_for(name)


@router.post("/projects/{name}/ssh")
def create_ssh(name: str, body: SshIn):
    return connections.create(name, body.model_dump())


@router.put("/projects/{name}/ssh/{cid}")
def update_ssh(name: str, cid: int, body: SshIn):
    return connections.update(name, cid, body.model_dump())


@router.delete("/projects/{name}/ssh/{cid}")
def delete_ssh(name: str, cid: int):
    return connections.delete(name, cid)


@router.post("/projects/{name}/ssh/{cid}/test")
async def test_ssh(name: str, cid: int):
    return await connections.test(name, cid)


@router.post("/projects/{name}/ssh/{cid}/connect")
async def connect_ssh(name: str, cid: int):
    return await connections.connect(name, cid)


@router.post("/projects/{name}/ssh/{cid}/disconnect")
async def disconnect_ssh(name: str, cid: int, runner: Runner = Depends(get_runner)):
    return await connections.disconnect(name, cid, runner)


@router.post("/projects/{name}/ssh/{cid}/open")
def open_ssh(name: str, cid: int):
    return connections.open_in_terminal(name, cid)
