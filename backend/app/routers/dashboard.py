from fastapi import APIRouter
from app.services import catalog_service as svc

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard")
def dashboard():
    return svc.dashboard()

@router.get("/dashboard/extended")
def dashboard_extended():
    # 历史遗留路径：与 /dashboard 同源返回，不再叠加第二套计数逻辑。
    return svc.dashboard()

