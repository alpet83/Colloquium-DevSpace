# /agent/lib/esctext.py, updated 2025-07-18 13:00 EEST
import re

color_scheme = "cli"  # Default to CLI, can be set to 'html' or 'none'

def colorize_msg(msg):
    global color_scheme
    if color_scheme == "cli":
        # Handle RGB colors (~C38#RRGGBB or ~C48#RRGGBB)
        msg = re.sub(
            r"~C([34]8)#([0-9A-Fa-f]{6})",
            lambda m: f"\033[{m.group(1)};2;{int(m.group(2)[:2], 16)};{int(m.group(2)[2:4], 16)};{int(m.group(2)[4:], 16)}m",
            msg
        )
        # Handle standard ANSI codes (~C00 to ~C110)
        cmsg = re.sub(r"~C(1*\d\d)", r"\033[\1m", msg)
        # Add reset if any color codes were replaced
        if cmsg != msg:
            cmsg += "\033[0m\033[49m"
        return cmsg
    elif color_scheme == "html":
        msg = msg.replace("~C00", "</font>")
        msg = re.sub(r"~C(1*\d\d)", r"<font class=cl\1>", msg)
        if "<font" in msg and "</font>" not in msg:
            msg += "</font>"
        return msg
    return msg

def format_color(fmt, *args):
    global color_scheme
    fmt = re.sub(r"(%[-\d]*s)", r"~C92\1~C00", fmt)  # Strings in green
    fmt = re.sub(r"(%[-\dl]*[du])", r"~C95\1~C00", fmt)  # Numbers in magenta
    fmt = re.sub(r"(%[-\.\d]*[fF])", r"~C95\1~C00", fmt)  # Floats in magenta
    fmt = re.sub(r"(%[-\.\d]*[gG])", r"~C95\1~C00", fmt)  # Scientific in magenta
    msg = fmt if not args else fmt % args
    if color_scheme == "none":
        return msg
    return colorize_msg(msg)

def format_uncolor(fmt, *args):
    msg = fmt % args if args else fmt
    return re.sub(r"~C\d\d", "", msg)

def esc_color_styles(tags=True):
    # Map for text colors (~C30–~C37, ~C90–~C97, ~C00)
    TEXT_COLORS = [
        "00:gray",          # Reset
        "30:000000",        # Black
        "31:800000",        # Dark red
        "32:008000",        # Dark green
        "33:808000",        # Dark yellow
        "34:000080",        # Dark blue
        "35:800080",        # Dark magenta
        "36:008080",        # Dark cyan
        "37:c0c0c0",        # Light gray
        "90:808080",        # Bright black (dark gray)
        "91:FF8080",        # Bright red
        "92:lime",          # Bright green
        "93:yellow",        # Bright yellow
        "94:8080FF",        # Bright blue
        "95:FF80FF",        # Bright magenta
        "96:00FFFF",        # Bright cyan
        "97:white"          # Bright white
    ]
    # Map for background colors (~C40–~C47)
    BG_COLORS = [
        "40:000000",        # Background black
        "41:800000",        # Background dark red
        "42:008000",        # Background dark green
        "43:808000",        # Background dark yellow
        "44:000080",        # Background dark blue
        "45:800080",        # Background dark magenta
        "46:008080",        # Background dark cyan
        "47:c0c0c0"         # Background light gray
    ]
    # Map for text styles (~C01–~C08)
    TEXT_STYLES = [
        "01:font-weight:bold",           # Bold
        "02:font-weight:lighter",        # Dim
        "03:font-style:italic",          # Italic
        "04:text-decoration:underline",  # Underline
        "05:text-decoration:blink",      # Blink
        "06:font-weight:bold",           # Rapid blink (same as bold for simplicity)
        "07:filter:invert(100%)",        # Reverse video
        "08:visibility:hidden",          # Conceal
        "09:text-decoration:line-through" # Strikethrough
    ]
    result = "\t<style type='text/css'>\n" if tags else ""
    result += "".join(f" .cl{code} {{ color: #{color}; }}\n" for code, color in (item.split(":") for item in TEXT_COLORS))
    result += "".join(f" .cl{code} {{ background-color: #{color}; }}\n" for code, color in (item.split(":") for item in BG_COLORS))
    result += "".join(f" .cl{code} {{ {style}; }}\n" for code, style in (item.split(":") for item in TEXT_STYLES))
    if tags:
        result += "\t</style>\n"
    return result