from fastapi import APIRouter

from workbench.api.schemas import ProjectIn, ProjectSourceIn, ProjectSourcesIn
from workbench.services import projects

router = APIRouter(tags=["projects"])


@router.get("/projects")
def list_projects():
    return projects.scan_projects()


@router.post("/projects", status_code=201)
def create_project(body: ProjectIn):
    return projects.create_project(body.name, [s.model_dump() for s in body.sources])


@router.put("/projects/{name}/sources")
def save_sources(name: str, body: ProjectSourcesIn):
    return projects.save_sources(name, [s.model_dump() for s in body.sources])


@router.get("/project-folders/local")
def local_folder(path: str = ""):
    return projects.local_folder(path)


@router.put("/project-folders/recent")
def remember_folder(body: ProjectSourceIn):
    projects.remember_folder(body.ssh_id, body.path)
    return {"ok": True}


@router.get("/project-folders/ssh")
def folder_connections():
    return projects.folder_connections()


@router.get("/project-folders/ssh/{ssh_id}")
async def remote_folder(ssh_id: int, path: str = "/"):
    return await projects.remote_folder(ssh_id, path)
