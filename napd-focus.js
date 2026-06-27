// napd-focus — KWin script: push the active window to napd over D-Bus.
// Pid is sent as a string to avoid int/double ambiguity across KWin versions.
function send(c) {
    if (!c) return;
    try {
        callDBus("ai.palabra.NapD", "/ai/palabra/NapD", "ai.palabra.NapD",
                 "FocusChanged",
                 "" + c.pid,
                 "" + (c.resourceClass || ""),
                 "" + (c.caption || ""));
    } catch (e) {
        print("napd-focus: callDBus failed: " + e);
    }
}

if (workspace.windowActivated) {            // Plasma 6
    workspace.windowActivated.connect(send);
    send(workspace.activeWindow);
} else if (workspace.clientActivated) {     // Plasma 5 fallback
    workspace.clientActivated.connect(send);
    send(workspace.activeClient);
}
