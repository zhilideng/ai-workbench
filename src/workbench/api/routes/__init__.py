"""按资源划分的路由。新增一组接口：在这里加一个模块并放进 ROUTERS。"""
from workbench.api.routes import config, projects, quota, runs, ssh, tasks, uploads

ROUTERS = [config.router, projects.router, ssh.router, tasks.router, runs.router, uploads.router, quota.router]
