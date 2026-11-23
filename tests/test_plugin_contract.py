from __future__ import annotations

import importlib.util
from contextlib import nullcontext
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).parents[1]


def test_release_versions_are_consistent():
    import tomllib
    from icloud_caldav import __version__
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    local = next(package for package in lock["package"] if package["name"] == "hermes-icloud-caldav")
    assert manifest["version"] == project["project"]["version"] == local["version"] == __version__ == "0.3.0"


class FakeContext:
    def __init__(self):
        self.tools = []
        self.hooks = []

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_hook(self, name, callback, **kwargs):
        self.hooks.append((name, callback, kwargs))


def test_manifest_and_registration_match():
    from icloud_caldav.schemas import MUTATIONS

    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    spec = importlib.util.spec_from_file_location(
        "caldav_plugin_entry", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    ctx = FakeContext()
    module.register(ctx)
    names = {tool["name"] for tool in ctx.tools}
    assert names == set(manifest["provides_tools"])
    assert names == {
        "icloud_calendars_list", "icloud_events_query", "icloud_event_get",
        "icloud_event_create", "icloud_event_update", "icloud_event_delete", "icloud_event_move",
    }
    for tool in ctx.tools:
        encoded = json.dumps(tool["schema"]).lower()
        assert "password" not in encoded and "credential" not in encoded
    assert not ctx.hooks  # Writes confirm in their handler, after host hook dispatch.
    for tool in ctx.tools:
        if tool["name"] in MUTATIONS:
            assert 'operation_id' in tool['schema']['parameters']['required']

    secret = manifest["config_schema"]["app_password"]
    assert secret["type"] == "secret"
    assert secret["env"] == "ICLOUD_CALDAV_APP_PASSWORD"
    assert manifest["config_schema"]["server_url"]["default"] == "https://caldav.icloud.com"


def test_dashboard_backend_and_desktop_component_are_importable_contracts():
    dashboard_manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text())
    assert dashboard_manifest == {"name": "icloud-caldav", "api": "plugin_api.py"}
    spec = importlib.util.spec_from_file_location("caldav_dashboard_api", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    paths = {route.path for route in module.router.routes}
    assert {"/status", "/discover", "/permissions", "/disconnect"} <= paths

    js = (ROOT / "desktop" / "plugin.js").read_text()
    assert "registerSettingsPage" in js
    assert "gatewayActivationEpoch" in js
    assert "localStorage" not in js and "sessionStorage" not in js
    assert "defaultEnabled: false" in js
    assert "https://caldav.icloud.com" in js


@pytest.mark.parametrize('operation', ['create', 'update', 'delete', 'move'])
def test_mutations_require_a_fresh_confirmation_on_each_call(operation, monkeypatch):
    from icloud_caldav import runtime, tools

    prompts, writes = [], []
    answers = iter(['accept', 'decline', 'cancel'])
    approval = ModuleType('tools.approval_prompt')

    def confirm(message, description, **options):
        assert options.get('remember_key') is None
        prompts.append(message)
        return next(answers)

    monkeypatch.setattr(approval, 'request_elicitation_consent', confirm, raising=False)
    monkeypatch.setitem(sys.modules, 'tools.approval_prompt', approval)
    svc = SimpleNamespace(**{f'{operation}_event': lambda **kw: writes.append(kw) or {'status': 'completed'}})
    opened = []

    def open_service():
        opened.append(len(prompts))
        return nullcontext(svc)

    monkeypatch.setattr(runtime, 'prepare_service', lambda: open_service)
    handler = tools.HANDLERS[f'icloud_event_{operation}']
    args = {'calendar_id': 'https://caldav.icloud.com/synthetic/', 'operation_id': 'test-1',
            'explicit_user_request': True}
    if operation == 'create':
        args.update(title='Review', start='2026-11-03', end='2026-11-04')
    else:
        args.update(href=args['calendar_id'] + 'seed.ics', etag='"synthetic-v1"')
    if operation == 'move':
        args['destination_calendar_id'] = 'https://caldav.icloud.com/synthetic-destination/'

    assert json.loads(handler({**args, 'explicit_user_request': 1}))['error'] == 'permission'
    assert json.loads(handler(args))['ok'] is True
    for _ in range(2):
        assert json.loads(handler(args))['error'] == 'permission'
    assert len(prompts) == 3
    assert opened == [1]  # No transport or transport deadline before consent.
    assert writes == [args]
    assert all(operation in prompt and 'test-1' in prompt for prompt in prompts)
    if operation == 'move':
        assert all(args['destination_calendar_id'] in prompt for prompt in prompts)


def test_missing_confirmation_support_blocks_writes_but_not_reads(monkeypatch):
    from icloud_caldav import runtime, tools

    monkeypatch.setitem(sys.modules, 'tools.approval_prompt', None)
    monkeypatch.setattr(runtime, 'prepare_service', lambda: pytest.fail('missing consent should block preparation'))
    svc = SimpleNamespace(query_events=lambda **_: [], create_event=lambda **_: pytest.fail('unexpected write'))
    monkeypatch.setattr(tools, '_service', lambda: nullcontext(svc))
    assert json.loads(tools.event_create({'explicit_user_request': True}))['error'] == 'permission'
    assert json.loads(tools.events_query({})) == {'ok': True, 'result': []}
