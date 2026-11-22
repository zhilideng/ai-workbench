from fastapi import APIRouter

from workbench import __version__
from workbench.api.schemas import SettingsIn
from workbench.core.settings import get_settings
from workbench.engines import REGISTRY
from workbench.services import projects

router = APIRouter(tags=["config"])


@router.get("/config")
def get_config():
    s = get_settings()
    root = s.projects_root
    out = {"version": __version__, "projects_root": str(root) if root else "",
           "projects_root_locked": s.projects_root_locked, "data_dir": str(s.paths.data),
           "engines": {name: e.available() for name, e in REGISTRY.items()},
           "max_concurrent": s.max_concurrent}
    for e in REGISTRY.values():
        out.update(e.client_config())
    return out


@router.put("/settings")
def put_settings(body: SettingsIn):
    if body.projects_root is not None:
        projects.set_projects_root(body.projects_root)
    return get_config()
