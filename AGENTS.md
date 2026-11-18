# Phone Agent repository guidance

Keep the cellular call agent and app MCP entry points independent. Device
selection and ADB profiles belong under `device/`. Provider and conversation
logic belongs under `call_agent/`. Common app UI tools belong under
`app_control/`.

Configure the MCP client with `phone-agent apps --profile PROFILE`. For a
multi-step app task, call `begin_app_work` and release it with `end_app_work`
when done. The lock also releases when that stdio process exits. Get the
current screen after launching an app, waking the phone, rotating it, or
changing screens. Find by text, resource ID, or accessibility description
before using measured coordinates. If the UI tree lacks the information, use
`capture_screen`. Disabled controls and off-screen nodes can appear in a
hierarchy dump. Check the current state after a consequential action.

Use `get_device_status` to distinguish a sleeping phone from a disconnected
device. The app interface targets the main display. Handle app sign-in,
permissions, and device lock prompts in the context of the operator's request.

Operator-authored app skills and app-specific workflow scripts live locally
under `skills/`, which is excluded from Git. Keep phone profiles, credentials,
capture files, and account data outside the source tree. Common interfaces
and reusable device scripts can be maintained here.
