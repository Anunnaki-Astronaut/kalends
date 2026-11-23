"""Hermes iCloud CalDAV unified plugin registration."""

try:
    from .icloud_caldav.schemas import SCHEMAS
    from .icloud_caldav.tools import HANDLERS
except ImportError:
    if __package__:
        raise
    from icloud_caldav.schemas import SCHEMAS
    from icloud_caldav.tools import HANDLERS


def register(ctx):
    for name, schema in SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset="icloud_caldav",
            schema=schema,
            handler=HANDLERS[name],
            emoji="📅",
        )
