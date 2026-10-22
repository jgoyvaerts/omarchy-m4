-- M4 desktop profile for Hyprland (selected by the m4-hyprland session entry: `start-hyprland -- --config /usr/share/m4/hyprland.lua`).
-- It loads the owner's own Hyprland config first and then pins the feature set the M4 GPU stack has been tested with. No user file is edited.
-- Kernel/Mesa admission remains authoritative; this is not an app sandbox.
local home = assert(os.getenv("HOME"), "HOME missing")
local named_desktop = os.getenv("XDG_CURRENT_DESKTOP") or ""
-- The normal startup callback (UWSM/DBus imports, the Omarchy shell supervisor) is preserved: the user's file runs unchanged.
dofile(home .. "/.config/hypr/hyprland.lua")
-- Normal defaults set Hyprland alone; preserve the session's name on reload.
if named_desktop ~= "" then hl.env("XDG_CURRENT_DESKTOP", named_desktop) end
hl.config({
  input = { repeat_rate = 40, repeat_delay = 250 },
  general = { border_size = 0, allow_tearing = false },
  animations = { enabled = false },
  decoration = {
    rounding = 0,
    active_opacity = 1.0, inactive_opacity = 1.0, fullscreen_opacity = 1.0,
    blur = { enabled = false },
    shadow = { enabled = false },
    glow = { enabled = false },
    motion_blur = { enabled = false },
    screen_shader = "",
  },
  group = { groupbar = { enabled = false, blur = false } },
  render = {
    cm_enabled = false,
    use_fp16 = 0,
    cm_auto_hdr = 0,
    keep_unmodified_copy = 0,
    direct_scanout = 0,
    use_shader_blur_blend = false,
  },
  misc = { vrr = 0, session_lock_blur = false },
  cursor = { no_hardware_cursors = 1 },
  xwayland = { enabled = false },
})
-- default.hypr.envs changes QT_QPA_PLATFORM to wayland;xcb. Restore the tested client selection after that import.
hl.env("QT_QPA_PLATFORM", "wayland")
hl.env("QT_WAYLAND_CLIENT_BUFFER_INTEGRATION", "wayland-egl")
hl.env("QSG_RHI_BACKEND", "opengl")
hl.env("QSG_RENDER_LOOP", "basic")
hl.env("GDK_BACKEND", "wayland")
hl.env("GSK_RENDERER", "gl")
hl.env("GDK_DISABLE", "vulkan")
hl.config({ misc = { disable_xdg_env_checks = true } })

-- The keyboard light is the standard LED class device (apple::kbd_backlight) and Omarchy's own helper drives it. The MacBook top row has
-- no keyboard-light keys, so add SUPER + brightness keys; Omarchy's XF86Kbd* bindings stay as they are. SHIFT + brightness drives the
-- keyboard light as in omarchy-mac (it replaces Omarchy's display minimum/maximum; plain and ALT + brightness keep the display).
local o = _G.o
if o then
  o.bind("SUPER + XF86MonBrightnessUp", "Keyboard backlight up", "omarchy-brightness-keyboard up", { repeating = true })
  o.bind("SUPER + XF86MonBrightnessDown", "Keyboard backlight down", "omarchy-brightness-keyboard down", { repeating = true })
  o.bind("SUPER + SHIFT + XF86MonBrightnessUp", "Keyboard backlight cycle", "omarchy-brightness-keyboard cycle")
  if o.rebind then
    o.rebind("SHIFT + XF86MonBrightnessUp", "Keyboard brightness up", "omarchy-brightness-keyboard up", { locked = true, repeating = true })
    o.rebind("SHIFT + XF86MonBrightnessDown", "Keyboard brightness down", "omarchy-brightness-keyboard down", { locked = true, repeating = true })
  end
end

-- The display driver marks the 60 Hz mode preferred and the user config asks for "preferred": run the internal panel at its highest rate like
-- the greeter. Scoped to eDP-1, after the user config, so rules for external displays still apply; inside pcall so an error cannot skip the above.
pcall(function()
  hl.monitor({ output = "eDP-1", mode = "highrr", position = "auto", scale = 2 })
end)
