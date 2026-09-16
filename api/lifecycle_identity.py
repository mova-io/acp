"""Stable, tenant-scoped source identity for lifecycle state; never infer it from a path."""
from __future__ import annotations

TERMINAL = ('Already archived', 'Archived', 'Deleted')
PROTECTED = (*TERMINAL, 'Exempted')


def source_binding(item: dict) -> dict:
    provider=str(item.get('source') or '').strip().lower()
    return {'provider': provider,'item_id': item.get('drive_file_id'),
            'namespace': item.get('drive_account_id') if provider=='drive' else item.get('drive_id')}


def source_identity(owner: str, provider: str, item: dict) -> tuple[str, str, str, str] | None:
    tenant = str(owner or '').strip().lower()
    provider = str(provider or '').strip().lower()
    item_id = str(item.get('drive_file_id') or '').strip()
    # Provider account and drive ids are opaque, case-sensitive identifiers, not email aliases.
    if provider == 'drive':
        namespace = str(item.get('drive_account_id') or '').strip()
    elif provider in ('sharepoint', 'onedrive'):
        namespace = str(item.get('drive_id') or '').strip()
    else:
        return None
    if not tenant or not namespace or not item_id:
        return None
    return tenant, provider, namespace, item_id
