from fastapi import APIRouter

router = APIRouter()

@router.get("/example")
def get_example():
    return {"example": "This is an example route"}
