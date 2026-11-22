# -*- coding: utf-8 -*-
"""Look of the panel: colour schemes and window style.

Colour scheme: "Follow Symbol Bag" (default) or any scheme below. The nine bag
styles are also built in, so they work without Symbol Bag; "QGIS Neutral" and
"Graphite" are calmer schemes for daily work.

Window style: header (gradient / solid / minimal), corners (rounded / square)
and density (comfortable / compact).
"""

import os

from qgis.PyQt.QtGui import QColor

KEYS = ("h1", "h2", "accent", "body", "card", "text", "sub", "htext", "dark")

SCHEMES = {
    "neutral":  dict(label="QGIS Neutral", h1="#5f7d95", h2="#4a6378", accent="#3d7ab8", body="#f3f4f6",
                     card="#ffffff", text="#1f2933", sub="#6b7785", htext="#ffffff", dark=False, bag="white"),
    "graphite": dict(label="Graphite (dark)", h1="#3a4150", h2="#2b313c", accent="#5aa2ff", body="#1f232b",
                     card="#2a2f39", text="#e4e8ee", sub="#9aa4b2", htext="#e4e8ee", dark=True, bag="midnight"),
    "sky":      dict(label="Sky Blue", h1="#79b4f5", h2="#4f8fe3", accent="#2f74d8", body="#eef5fd",
                     card="#ffffff", text="#1f2d3d", sub="#647487", htext="#ffffff", dark=False, bag="sky"),
    "peach":    dict(label="Peach", h1="#f8b09d", h2="#ef8a72", accent="#e2694c", body="#fdf2ee",
                     card="#ffffff", text="#3a2621", sub="#8a6c64", htext="#ffffff", dark=False, bag="peach"),
    "mint":     dict(label="Mint", h1="#86d4ae", h2="#56b88c", accent="#2f9a68", body="#eef8f3",
                     card="#ffffff", text="#1c3328", sub="#5e7a6c", htext="#ffffff", dark=False, bag="mint"),
    "lavender": dict(label="Lavender", h1="#bea8e6", h2="#9a7fd3", accent="#7b55c7", body="#f4f0fb",
                     card="#ffffff", text="#2a2140", sub="#6f6488", htext="#ffffff", dark=False, bag="lavender"),
    "sakura":   dict(label="Sakura", h1="#f6b2c8", h2="#ea86a6", accent="#d55a82", body="#fdf1f5",
                     card="#ffffff", text="#3b2230", sub="#8c6676", htext="#ffffff", dark=False, bag="sakura"),
    "lemon":    dict(label="Lemon", h1="#f8dc7c", h2="#f0c64c", accent="#b98a00", body="#fdf9ea",
                     card="#ffffff", text="#3a3214", sub="#7f7350", htext="#4a3c08", dark=False, bag="lemon"),
    "kraft":    dict(label="Kraft", h1="#c49a6c", h2="#a87b4f", accent="#8a5f35", body="#f7efe3",
                     card="#fffaf2", text="#3a2a1a", sub="#7d6650", htext="#ffffff", dark=False, bag="kraft"),
    "white":    dict(label="Classic White", h1="#ffffff", h2="#eef1f5", accent="#4b6584", body="#f6f7f9",
                     card="#ffffff", text="#1f2933", sub="#6b7785", htext="#1f2933", dark=False, bag="white"),
    "midnight": dict(label="Midnight", h1="#34405a", h2="#252e42", accent="#6ea8ff", body="#1b2231",
                     card="#263043", text="#e6ebf2", sub="#9aa6b8", htext="#e6ebf2", dark=True, bag="midnight"),
}
SCHEME_ORDER = ["neutral", "graphite", "sky", "peach", "mint", "lavender", "sakura", "lemon", "kraft", "white",
                "midnight"]
FOLLOW_BAG = "bag"
DEFAULT = {k: SCHEMES["neutral"][k] for k in KEYS}

HEADERS = [("gradient", "Gradient"), ("solid", "Solid"), ("minimal", "Minimal")]
CORNERS = [("rounded", "Rounded"), ("square", "Square")]
DENSITIES = [("comfortable", "Comfortable"), ("compact", "Compact")]
DEFAULT_LOOK = dict(header="gradient", corners="rounded", density="comfortable")


def _icon_path(name):
    return os.path.join(os.path.dirname(__file__), "icons", name).replace("\\", "/")


def scheme_palette(name):
    s = SCHEMES.get(name)
    return {k: s[k] for k in KEYS} if s else None


def bag_style_for(palette):
    """Symbol Bag style name matching a palette (for the embedded colour wheel)."""
    for s in SCHEMES.values():
        if all(s[k] == palette.get(k) for k in ("h1", "h2", "accent", "body")):
            return s["bag"]
    return None


def rgba(color, alpha):
    c = QColor(color)
    return "rgba({},{},{},{})".format(c.red(), c.green(), c.blue(), int(alpha))


def mix(a, b, t):
    a, b = QColor(a), QColor(b)
    return QColor(int(a.red() + (b.red() - a.red()) * t), int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t)).name()


def header_foreground(p, look):
    """Text / icon colour used on the header."""
    return p["text"] if look.get("header") == "minimal" else p["htext"]


def stylesheet(p, look=None):
    look = dict(DEFAULT_LOOK, **(look or {}))
    dark = p.get("dark", False)
    acc = p["accent"]
    line = rgba(acc, 60 if dark else 45)
    soft = rgba(acc, 45 if dark else 26)
    softer = rgba(acc, 28 if dark else 14)
    field_bg = mix(p["card"], p["body"], 0.35)
    alt = mix(p["card"], p["body"], 0.55)
    square = look["corners"] == "square"
    compact = look["density"] == "compact"
    r_lg, r_md, r_sm = (4, 3, 2) if square else (14, 10, 7)
    hfg = header_foreground(p, look)
    if look["header"] == "minimal":
        header_bg = "{card}; border: 1px solid {line}".format(card=p["card"], line=line)
        tile_bg = soft
    elif look["header"] == "solid":
        header_bg = p["h2"]
        tile_bg = rgba(hfg, 55 if hfg == "#ffffff" else 35)
    else:
        header_bg = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {}, stop:1 {})".format(p["h1"], p["h2"])
        tile_bg = rgba(hfg, 55 if hfg == "#ffffff" else 35)
    v = dict(body=p["body"], card=p["card"], text=p["text"], sub=p["sub"], accent=acc, line=line, soft=soft,
             softer=softer, alt=alt, field_bg=field_bg, hfg=hfg, hfg_soft=rgba(hfg, 200), header_bg=header_bg,
             tile_bg=tile_bg, accent_hover=QColor(acc).darker(112).name(), r_lg=r_lg, r_md=r_md, r_sm=r_sm,
             r_pill=3 if square else 11, tab_pad="4px 6px" if compact else "6px 6px",
             pill_pad="2px 8px" if compact else "3px 10px", input_pad="2px 5px" if compact else "3px 6px",
             btn_pad="4px 12px" if compact else "6px 14px", cell_pad="2px 5px" if compact else "4px 6px",
             name_size="10pt" if compact else "11pt", big_size="11.5pt" if compact else "13pt",
             chev_down=_icon_path("chev_down.svg"), chev_up=_icon_path("chev_up.svg"))
    return """
    QWidget#LMPRoot {{ background: {body}; color: {text}; }}
    QWidget#LMPRoot QLabel {{ color: {text}; background: transparent; }}

    QFrame#header {{ border-radius: {r_lg}px; background: {header_bg}; }}
    QFrame#header QLabel {{ color: {hfg}; }}
    QLabel#layerName {{ font-weight: 700; font-size: {name_size}; }}
    QLabel#layerMeta {{ color: {hfg_soft}; font-size: 8.5pt; }}
    QLabel#geomTile {{ background: {tile_bg}; border-radius: {r_md}px; }}
    QLabel#badge {{ background: {tile_bg}; border-radius: {r_sm}px; padding: 2px 9px; font-size: 8.5pt;
                    font-weight: 600; }}
    QLabel#badgeWarn {{ background: rgba(255,196,64,215); color: #4a3200; border-radius: {r_sm}px;
                        padding: 2px 9px; font-size: 8.5pt; font-weight: 600; }}
    QFrame#header QToolButton {{ border-radius: {r_sm}px; padding: 3px; }}
    QFrame#header QToolButton:hover {{ background: {tile_bg}; }}
    QFrame#header QToolButton::menu-indicator {{ image: none; width: 0px; }}

    QFrame#segment {{ background: {card}; border: 1px solid {line}; border-radius: {r_lg}px; }}
    QFrame#segment QToolButton {{ border: none; border-radius: {r_md}px; padding: {tab_pad}; margin: 1px;
                                  color: {sub}; font-weight: 600; }}
    QFrame#segment QToolButton:hover {{ background: {softer}; color: {text}; }}
    QFrame#segment QToolButton:checked {{ background: {accent}; color: #ffffff; }}
    QFrame#segment QToolButton:disabled {{ color: {line}; }}

    QToolButton#pill {{ border: 1px solid {line}; border-radius: {r_pill}px; padding: {pill_pad};
                        background: {card}; color: {sub}; }}
    QToolButton#pill:hover {{ background: {softer}; color: {text}; }}
    QToolButton#pill:checked {{ background: {soft}; border-color: {accent}; color: {accent}; font-weight: 600; }}

    QFrame#card, QLabel#card {{ background: {card}; border: 1px solid {line}; border-radius: {r_lg}px; }}
    QLabel#chip {{ background: {card}; border: 1px solid {line}; border-radius: {r_pill}px; padding: 2px 9px;
                   font-size: 8.5pt; color: {text}; }}
    QLabel#chipAccent {{ background: {soft}; border: 1px solid {accent}; border-radius: {r_pill}px;
                         padding: 2px 9px; font-size: 8.5pt; color: {accent}; font-weight: 600; }}
    QLabel#sectionTitle {{ color: {sub}; font-size: 8pt; font-weight: 700; letter-spacing: 0.6px; }}
    QLabel#muted {{ color: {sub}; }}
    QLabel#hint {{ color: {sub}; background: {card}; border: 1px dashed {line}; border-radius: {r_md}px;
                   padding: 8px 10px; }}
    QLabel#notice {{ color: {text}; background: {soft}; border-radius: {r_md}px; padding: 8px 10px; }}
    QLabel#bigValue {{ font-size: {big_size}; font-weight: 700; color: {text}; }}
    QLabel#tileLabel {{ font-size: 8pt; color: {sub}; }}
    QFrame#statTile {{ background: {card}; border: 1px solid {line}; border-radius: {r_md}px; }}

    QTableView, QListView, QTreeView {{ background: {card}; alternate-background-color: {alt};
        border: 1px solid {line}; border-radius: {r_md}px; color: {text};
        selection-background-color: {soft}; selection-color: {text}; gridline-color: {line}; }}
    QHeaderView::section {{ background: {field_bg}; color: {sub}; border: none;
        border-bottom: 1px solid {line}; border-right: 1px solid {softer}; padding: {cell_pad};
        font-weight: 600; font-size: 8.5pt; }}
    QTableCornerButton::section {{ background: {field_bg}; border: none; }}

    QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox, QFontComboBox {{ background: {card};
        border: 1px solid {line}; border-radius: {r_sm}px; padding: {input_pad}; color: {text};
        selection-background-color: {accent}; }}
    QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus {{ border: 1px solid {accent}; }}
    QComboBox::drop-down {{ border: none; width: 20px; subcontrol-origin: padding;
                            subcontrol-position: top right; }}
    QComboBox::down-arrow {{ image: url({chev_down}); width: 12px; height: 12px; }}
    QAbstractSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 18px;
                                   border: none; border-left: 1px solid {softer}; }}
    QAbstractSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; width: 18px;
                                     border: none; border-left: 1px solid {softer}; }}
    QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{ background: {softer}; }}
    QAbstractSpinBox::up-arrow {{ image: url({chev_up}); width: 10px; height: 10px; }}
    QAbstractSpinBox::down-arrow {{ image: url({chev_down}); width: 10px; height: 10px; }}
    QDoubleSpinBox, QSpinBox {{ padding-right: 20px; }}
    QGroupBox {{ background: {card}; border: 1px solid {line}; border-radius: {r_md}px; margin-top: 14px;
                 padding-top: 6px; color: {text}; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {text};
                        font-weight: 600; }}
    QCheckBox {{ color: {text}; spacing: 6px; }}

    QToolButton {{ border-radius: {r_sm}px; padding: 3px; color: {text}; }}
    QToolButton:hover {{ background: {softer}; }}
    QToolButton:checked {{ background: {soft}; }}
    QToolButton#textButton {{ color: {accent}; font-weight: 600; padding: 3px 8px; }}
    QPushButton#primary {{ background: {accent}; color: #ffffff; border: none; border-radius: {r_md}px;
                           padding: {btn_pad}; font-weight: 600; }}
    QPushButton#primary:hover {{ background: {accent_hover}; }}
    QPushButton#primary:disabled {{ background: {line}; color: {sub}; }}
    QPushButton#secondary {{ background: {card}; color: {accent}; border: 1px solid {line};
                             border-radius: {r_md}px; padding: {btn_pad}; font-weight: 600; }}
    QPushButton#secondary:hover {{ background: {softer}; }}
    QProgressBar {{ background: {softer}; border: none; border-radius: 4px; height: 8px; text-align: center;
                    color: transparent; }}
    QProgressBar::chunk {{ background: {accent}; border-radius: 4px; }}
    QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
    QMenu {{ background: {card}; color: {text}; border: 1px solid {line}; padding: 4px; }}
    QMenu::item {{ padding: 5px 22px 5px 22px; border-radius: {r_sm}px; }}
    QMenu::item:selected {{ background: {soft}; }}
    QMenu::separator {{ height: 1px; background: {line}; margin: 4px 8px; }}
    """.format(**v)
