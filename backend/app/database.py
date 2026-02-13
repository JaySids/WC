"""
Supabase client for clone record CRUD.
"""

from app.config import get_settings


def _get_client():
    """Get a Supabase client. Raises if credentials are missing."""
    settings = get_settings()
    url = settings.supabase_url
    key = settings.supabase_key
    if not url or not key:
        raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
    from supabase import create_client
    return create_client(url, key)


async def save_clone(data: dict) -> dict:
    """Insert a clone record. Returns the inserted row."""
    client = _get_client()
    result = client.table("clones").insert(data).execute()
    return result.data[0] if result.data else {}


async def update_clone(clone_id: str, data: dict) -> dict:
    """Update a clone record."""
    client = _get_client()
    result = client.table("clones").update(data).eq("id", clone_id).execute()
    return result.data[0] if result.data else {}


async def get_clones(limit: int = 20) -> list:
    """Get recent clones."""
    client = _get_client()
    result = (
        client.table("clones")
        .select("id, url, preview_url, status, sandbox_id, is_active, output_format, metadata, created_at")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data


async def get_clone(clone_id: str) -> dict:
    """Get a single clone by ID."""
    client = _get_client()
    result = (
        client.table("clones")
        .select("*")
        .eq("id", clone_id)
        .single()
        .execute()
    )
    return result.data


async def delete_clone(clone_id: str) -> bool:
    """Delete a clone record."""
    client = _get_client()
    client.table("clones").delete().eq("id", clone_id).execute()
    return True


async def sync_files_to_supabase(clone_id: str, files: dict):
    """Update the files in a clone's metadata. Preserves other metadata fields."""
    client = _get_client()
    result = client.table("clones").select("metadata").eq("id", clone_id).single().execute()
    current_metadata = (result.data or {}).get("metadata") or {}
    current_metadata["files"] = files
    client.table("clones").update({"metadata": current_metadata}).eq("id", clone_id).execute()


async def toggle_clone_active(clone_id: str, is_active: bool) -> dict:
    """Toggle a clone's is_active status."""
    client = _get_client()
    result = (
        client.table("clones")
        .update({"is_active": is_active})
        .eq("id", clone_id)
        .execute()
    )
    return result.data[0] if result.data else {}
