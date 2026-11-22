from fastapi import APIRouter

from workbench.services import projects

router = APIRouter(tags=["projects"])


@router.get("/projects")
def list_projects():
    return projects.scan_projects()
