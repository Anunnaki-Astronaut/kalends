# Changelog

## 0.3.0

- Open **Kalends** from the Desktop sidebar to browse a month, see a day's agenda, and open event details. Today's date and the selected date have separate markers. The calendar works with the keyboard.
- Manage the iCloud account and calendar permissions from Settings. Use Theme Maker to save Light and Dark palettes or start with a preset such as Indigo.
- Optional views show upcoming Sonarr and Radarr dates, the next scheduled run of selected Hermes jobs, and a Pirate Weather forecast. None of these is required for iCloud Calendar.
- Retry a calendar read without reloading the whole view. Desktop can skip certain timed events with no duration and warn that results are incomplete. Agent queries still report those events as errors.
- The plugin ID is still `icloud-caldav`. Old **Read and write** grants become **Read** and **Create**, not **Edit** or **Delete**. Before saving grants on an upgrade, read the [rollback instructions](docs/INSTALL.md#before-upgrading-or-rolling-back).
