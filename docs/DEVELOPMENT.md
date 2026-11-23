# How Kalends works

`desktop/plugin.js` adds Kalends to the sidebar and renders the calendar and settings. It uses the Hermes Desktop SDK to talk to the selected backend. It keeps the events you're viewing in memory and saves appearance preferences on the device.

`dashboard/plugin_api.py` checks who owns the session and what calendar access they've saved. In `icloud_caldav/`, the client talks to iCloud, the parser handles supported repeating events, and the permission store checks each operation. Hermes Agent tools use that same service. Put real app-specific passwords in the Hermes settings form, never in fixtures or sample commands.

## Read and write boundaries

- Month and event details in Desktop are read-only. The agent tools have separate permission and approval checks for writes.
- Reads are bounded by dates, response size, and recurrence work. A read error is not an empty calendar.
- The backend uses conditional writes and verifies the result. A lost response can leave the result uncertain; don't retry an uncertain write blindly. Recurring event and invitation mutations are not supported.
- Forecast requests are separate from calendar requests. With consent enabled, the backend sends the location you enter to Pirate Weather; it does not use calendar location or a device's inferred location.
- Optional media routes read the configured private-LAN feeds with separate consent. The automation view reads next-run metadata for selected, enabled Hermes jobs; it does not execute or modify them.

## When a calendar read is incomplete

Desktop can skip certain timed events whose start and end are the same, then warn that the results are incomplete. Other malformed events or permission failures still cause an error. The shared service and Agent queries remain strict by default. See `tests/test_stage2_query.py` for examples.

## Run the tests

The tests and screenshots use made-up accounts, events, and server responses. Don't use a real account to run the tests. Use a disposable environment with an empty `HERMES_HOME` and no network access once the dependencies are installed. Some tests need loopback access to a server running inside that environment.

From the repo root, install the package and test dependencies into a disposable Python 3.11+ environment:

```bash
python -m pip install . fastapi pytest PyYAML
```

Run the standalone tests in that environment:

```bash
python -m pytest -q -k 'not test_owner_routes_no_store_admission_and_stale_results'
```

For the full Python suite, put a compatible [Hermes Agent source checkout](https://github.com/NousResearch/hermes-agent) on `PYTHONPATH` and run `python -m pytest -q`. The host integration test imports `hermes_cli.plugin_host_http_auth`; that module was checked against Hermes Agent v0.23.3. Don't point the tests at your live Hermes profile.

From the repo root with Node.js, you can run the standalone UI fixtures too:

```bash
node tests/ui_ownership.cjs
node tests/bulk_permissions.cjs
node --experimental-vm-modules tests/indigo_ui.cjs
```
