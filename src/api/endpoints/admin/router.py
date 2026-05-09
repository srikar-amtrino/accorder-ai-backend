from typing import Any, Dict

from fastapi import APIRouter

from src.dependencies import get_service_container

router = APIRouter(tags=["Session Management"])


@router.get("/sessions/")
async def list_sessions() -> Dict[str, Any]:
    """List all active sessions and their status."""

    service_container = get_service_container()
    session_manager = service_container.session_manager

    sessions = session_manager.list_sessions()
    stats = session_manager.get_total_stats()

    return {
        "sessions": sessions,
        "statistics": stats,
    }


@router.get("/sessions/{session_id}")
async def get_session_info(session_id: str) -> Dict[str, Any]:
    """Get detailed information about a specific session."""

    service_container = get_service_container()
    session_manager = service_container.session_manager

    session_info = session_manager.get_session_info(session_id)
    if not session_info:
        return {"error": "Session not found", "session_id": session_id}

    return session_info


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str) -> Dict[str, Any]:
    """Delete a specific session and all its data."""

    service_container = get_service_container()
    session_manager = service_container.session_manager

    deleted = session_manager.delete_session(session_id)

    if deleted:
        return {"status": "deleted", "session_id": session_id, "message": f"Session {session_id} and all associated data deleted"}
    else:
        return {"status": "error", "session_id": session_id, "message": f"Session {session_id} not found"}


@router.post("/sessions/cleanup")
async def cleanup_expired_sessions() -> Dict[str, Any]:
    """Manually trigger cleanup of expired sessions."""

    service_container = get_service_container()
    session_manager = service_container.session_manager

    cleaned_count = await session_manager.cleanup_expired_sessions()

    return {
        "cleaned_sessions": cleaned_count,
        "message": f"Cleaned up {cleaned_count} expired sessions",
        "ttl_minutes": session_manager.settings.session_ttl_minutes,
    }


@router.get("/health/")
async def health_check() -> Dict[str, Any]:
    """Health check endpoint."""

    service_container = get_service_container()

    return {
        "status": "healthy",
        "services": {
            "session_manager": "active",
            "ingestion_service": "active",
            "retrieval_service": "active",
            "llm_models": "active",
        },
        "statistics": service_container.session_manager.get_total_stats(),
    }
