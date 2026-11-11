"""Pure page-readiness policy for the disposable Windows UI harness."""

def nsis_ui_action(controls, *, uninstaller, fresh, directory_selected, owner_observed, selected_uninstall, sent):
    """Plan one notification only after the expected page is fully available."""
    active = [item for item in controls if item["visible"] and item["enabled"]]
    buttons = [item for item in active if item["class"] == "Button"]
    header = next((item["text"] for item in active if item["class"] == "Static" and item["id"] == 1201 and item["text"].startswith(("Welcome to", "Completing"))), "") or next((item["text"] for item in active if item["class"] == "Static" and item["id"] == 1037), "")
    radios = [item for item in buttons if item.get("style") in (4, 9)]
    if uninstaller:
        allowed, phase = ("Uninstall", "Close"), "uninstaller"
    elif header.startswith("Welcome to"):
        allowed, phase = ("Next >", "Next"), "welcome"
    elif not fresh and header == "Already Installed":
        # The header appears before nsDialogs creates its radios. A queued Next
        # from Welcome must never advance this partially constructed page.
        if len(radios) != 2:
            return None
        if not (radios[0].get("checked") == 0 and radios[1].get("checked") == 1):
            return None if "select-uninstall" in sent else ("select-uninstall", radios[1], radios)
        allowed, phase = ("Next >", "Next"), "maintenance"
    elif (fresh and directory_selected) or (not fresh and owner_observed and selected_uninstall):
        allowed, phase = ("Next >", "Next", "Install", "Finish", "Close"), header
    else:
        return None
    for button in buttons:
        label = button["text"].replace("&", "").strip()
        token = f"{phase}:{label}"
        if label in allowed and token not in sent:
            return token, button, []
    return None


