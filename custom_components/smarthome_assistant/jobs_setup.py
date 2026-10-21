"""Home Assistant persistence and event scheduling for SmartHome jobs."""
from __future__ import annotations

from homeassistant.components import persistent_notification
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.helpers.event import async_track_point_in_utc_time

from .jobs import DOMAIN, JobsManager, STORAGE_VERSION
from .timer_delivery import configure_delivery
from .job_websocket import async_register_websocket_commands

async def async_setup_jobs(hass):
    """Load persistent jobs and register authenticated WebSocket commands."""
    if DOMAIN in hass.data:
        if not getattr(hass.data[DOMAIN], "_unified_integration", False):
            raise RuntimeError("Remove the legacy smarthome_jobs YAML configuration and restart HA; stored jobs are preserved")
        return True

    def notify(job, reason):
        labels = {
            "missed": "A scheduled action was missed. It was not run.",
            "unknown": "A scheduled action may have run, but its result is unknown. It will not be repeated.",
            "failed": "A scheduled action failed.",
            "undelivered": "A timer expired without being delivered to its satellite.",
        }
        name = f"Timer {job['name']} expired without delivery." if reason == "undelivered" and job.get("name") else labels[reason]
        persistent_notification.async_create(
            hass,
            name,
            title="SmartHome job update",
            notification_id=f"{DOMAIN}_{job['id']}_{reason}",
        )

    store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.storage")
    manager = JobsManager(
        hass,
        store,
        now=dt_util.utcnow,
        # HA runs unmarked synchronous handlers in its executor. These handlers
        # enqueue async jobs and must run on HA's event loop instead.
        schedule_at=lambda handler, when: async_track_point_in_utc_time(hass, callback(handler), when),
        notify=notify,
        clear_notice=lambda job: persistent_notification.async_dismiss(hass, f"{DOMAIN}_{job['id']}_undelivered"),
    )
    await manager.async_load()
    manager._unified_integration = True
    hass.data[DOMAIN] = manager
    delivery = configure_delivery(hass, manager, {})
    manager.delivery_worker = delivery
    async_register_websocket_commands(hass)

    @callback
    def warn_legacy_jobs(_event):
        manager.delivery_worker.start()
        legacy = sorted(state.entity_id for state in hass.states.async_all()
                        if state.entity_id.startswith("switch.schedule_assistant_job_"))
        if legacy:
            persistent_notification.async_create(
                hass,
                "Existing Scheduler Component assistant jobs need review before "
                "removing that HACS integration. They were not migrated: "
                + ", ".join(legacy),
                title="Review legacy SmartHome schedules",
                notification_id=f"{DOMAIN}_legacy_schedules",
            )

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, warn_legacy_jobs)
    if getattr(hass, "is_running", False):
        manager.delivery_worker.start()

    @callback
    def stop_delivery(_event):
        hass.async_create_task(manager.delivery_worker.stop())

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_delivery)
    return True


async def async_configure_outputs(hass, bindings):
    """Apply UI bindings without restarting the durable job manager.

    A conversation reload must not reset delivery ownership, sequence numbers,
    callbacks or receipt acknowledgement state. Binding removal/changes wait
    until the affected accepted timers are terminal.
    """
    manager = hass.data[DOMAIN]
    old = manager.delivery_worker
    if old.bindings == bindings:
        return
    await manager.async_validate_output_bindings(bindings)
    # Validate before stopping the existing worker.
    replacement = configure_delivery(hass, manager, bindings)
    await old.stop()
    manager.delivery_worker = replacement
    if getattr(hass, "is_running", False):
        replacement.start()
