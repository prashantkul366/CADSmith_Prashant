"""Realistic mechanical parts for the mock provider.

What a CAD engineer actually asks for: bearing housings, manifolds, pulleys,
flanges - not primitives.  Each part carries the CadQuery a competent model
would write, a first attempt containing one plausible mistake, and the words
a Judge would use to reject it.

Every part here was executed in the OpenCASCADE kernel and its three-view
render inspected before being added.  The flawed variant is checked too: it
must build, so that the Judge rejects it on geometry rather than the Error
Refiner catching it as a crash.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class MockPart:
    id: str
    prompt: str
    keywords: list[str]
    plan: dict
    code_first: str
    code_fixed: str
    reject: str
    accept: str


PARTS: list[MockPart] = []


def _add(part_id, prompt, keywords, components, dims, code_fixed, flaw,
         reject, accept):
    old, new = flaw
    assert old in code_fixed, f"{part_id}: flaw anchor not found"
    PARTS.append(MockPart(
        id=part_id, prompt=prompt, keywords=keywords,
        plan={
            "description": prompt.split(".")[0].strip(),
            "components": components,
            "dimensions": {"key_dimensions": dims},
            "constraints": {"symmetry": None},
            "acceptance_criteria": {"volume_error_threshold_pct": 5,
                                    "bbox_iou_threshold": 0.90},
            "notes": "Scripted reply from app/tools/mock_parts.py.",
        },
        code_first=code_fixed.replace(old, new),
        code_fixed=code_fixed, reject=reject, accept=accept))


_add(
    'pillow_block',
    'A pillow block bearing housing for a 25mm shaft. Base 90mm long by 40mm wide by 12mm thick, with two 9mm mounting holes on 70mm centres. A cylindrical boss 50mm diameter rises to 45mm above the base with a 25mm through bore on the shaft axis, parallel to the base and 45mm above it.',
    ['pillow', 'block', 'bearing', 'housing', 'shaft', 'support', 'journal', 'mount a shaft', 'hold a shaft', 'rotating'],
    ['base plate', 'bearing boss', 'shaft bore', 'two mounting holes'],
    {'base_length': 90, 'base_width': 40, 'base_thickness': 12, 'boss_diameter': 50, 'bore_diameter': 25, 'shaft_height': 45},
    "import cadquery as cq\n\nbase_length = 90.0\nbase_width = 40.0\nbase_thickness = 12.0\nmount_hole_dia = 9.0\nmount_hole_spacing = 70.0\nboss_dia = 50.0\nbore_dia = 25.0\nshaft_height = 45.0\n\n# Base plate, drilled before anything is added on top: once the boss is\n# unioned on, '>Z' selects several faces including a curved one.\nresult = cq.Workplane('XY').box(\n    base_length, base_width, base_thickness, centered=(True, True, False)\n)\nresult = (\n    result.faces('>Z').workplane()\n    .pushPoints([(-mount_hole_spacing / 2.0, 0), (mount_hole_spacing / 2.0, 0)])\n    .hole(mount_hole_dia)\n)\n\n# Bearing boss: a cylinder lying on the Y axis, centred above the base\nboss = (\n    cq.Workplane('XZ')\n    .center(0, shaft_height)\n    .circle(boss_dia / 2.0)\n    .extrude(base_width / 2.0, both=True)\n)\nresult = result.union(boss)\n\n# Web joining boss to base\nweb = cq.Workplane('XY').box(\n    boss_dia, base_width, shaft_height, centered=(True, True, False)\n)\nresult = result.union(web)\n\n# Shaft bore through the boss\nbore = (\n    cq.Workplane('XZ')\n    .center(0, shaft_height)\n    .circle(bore_dia / 2.0)\n    .extrude(base_width, both=True)\n)\nresult = result.cut(bore)\n",
    ('shaft_height = 45.0', 'shaft_height = 35.0'),
    'The shaft axis sits 35mm above the base, not the 45mm specified — the overall height measures 60mm where it should be 70mm. The isometric view shows the boss sitting far too low over the base. Set shaft_height to 45.0.',
    'All constraints met. The kernel measurements match the requested dimensions and every feature named in the prompt is present in the rendered views. The solid is watertight.',
)

_add(
    'l_bracket',
    'An L-shaped mounting bracket 80mm wide. The horizontal leg is 100mm long and 10mm thick with two 9mm holes; the vertical leg is 80mm tall and 10mm thick with two 9mm holes. A triangular gusset 6mm thick reinforces the inside corner.',
    ['bracket', 'angle', 'l-shaped', 'l shaped', 'gusset', 'corner', 'mounting bracket', 'right angle'],
    ['horizontal leg', 'vertical leg', 'triangular gusset', 'four mounting holes'],
    {'width': 80, 'horizontal_length': 100, 'vertical_height': 80, 'thickness': 10, 'hole_diameter': 9},
    "import cadquery as cq\n\nwidth = 80.0\nhorizontal_length = 100.0\nvertical_height = 80.0\nthickness = 10.0\nhole_dia = 9.0\ngusset_thickness = 6.0\n\n# Horizontal leg lying on XY\nresult = cq.Workplane('XY').box(\n    horizontal_length, width, thickness, centered=(False, True, False)\n)\n\n# Vertical leg rising at the origin end\nvertical = cq.Workplane('XY').box(\n    thickness, width, vertical_height, centered=(False, True, False)\n)\nresult = result.union(vertical)\n\n# Triangular gusset in the inside corner, on the XZ plane\ngusset = (\n    cq.Workplane('XZ')\n    .polyline([(thickness, thickness),\n               (thickness + 45.0, thickness),\n               (thickness, thickness + 45.0)])\n    .close()\n    .extrude(gusset_thickness / 2.0, both=True)\n)\nresult = result.union(gusset)\n\n# Holes in the horizontal leg\nresult = (\n    result.faces('>Z').workplane(centerOption='CenterOfBoundBox')\n    .pushPoints([(20.0, -25.0), (20.0, 25.0)])\n    .hole(hole_dia)\n)\n\n# Holes in the vertical leg\nresult = (\n    result.faces('<X').workplane(centerOption='CenterOfBoundBox')\n    .pushPoints([(-25.0, 20.0), (25.0, 20.0)])\n    .hole(hole_dia)\n)\n",
    ('result = result.union(gusset)', '# gusset omitted'),
    'The gusset is missing. The prompt calls for a triangular rib reinforcing the inside corner, and the isometric view shows a bare right-angle joint with nothing bracing it. Build the triangular profile on the XZ plane and union it into the corner.',
    'All constraints met. The kernel measurements match the requested dimensions and every feature named in the prompt is present in the rendered views. The solid is watertight.',
)

_add(
    'pipe_flange',
    'A raised-face pipe flange: 160mm outer diameter, 50mm bore, 18mm thick. Eight 18mm bolt holes on a 125mm bolt circle. A raised sealing face 90mm diameter stands 2mm proud, and a hub 80mm diameter extends 25mm from the back.',
    ['flange', 'pipe', 'bolt circle', 'raised face', 'pcd', 'gasket face', 'ansi'],
    ['flange disc', 'raised sealing face', 'hub', 'eight bolt holes', 'central bore'],
    {'outer_diameter': 160, 'bore': 50, 'thickness': 18, 'bolt_count': 8, 'bolt_circle': 125, 'hub_diameter': 80},
    "import cadquery as cq\n\nouter_dia = 160.0\nbore_dia = 50.0\nflange_thickness = 18.0\nbolt_count = 8\nbolt_hole_dia = 18.0\nbolt_circle_dia = 125.0\nraised_face_dia = 90.0\nraised_face_height = 2.0\nhub_dia = 80.0\nhub_length = 25.0\n\n# Flange disc\nresult = cq.Workplane('XY').circle(outer_dia / 2.0).extrude(flange_thickness)\n\n# Raised sealing face on the front\nraised = (\n    cq.Workplane('XY').workplane(offset=flange_thickness)\n    .circle(raised_face_dia / 2.0).extrude(raised_face_height)\n)\nresult = result.union(raised)\n\n# Hub extending backwards\nhub = (\n    cq.Workplane('XY')\n    .circle(hub_dia / 2.0).extrude(-hub_length)\n)\nresult = result.union(hub)\n\n# Central bore through everything\nresult = (\n    result.faces('>Z').workplane(centerOption='CenterOfBoundBox')\n    .circle(bore_dia / 2.0)\n    .cutThruAll()\n)\n\n# Bolt holes on the bolt circle\nresult = (\n    result.faces('>Z').workplane(centerOption='CenterOfBoundBox')\n    .polarArray(bolt_circle_dia / 2.0, 0, 360, bolt_count)\n    .hole(bolt_hole_dia)\n)\n",
    ('bolt_count = 8', 'bolt_count = 6'),
    'Only six bolt holes are present; the prompt specifies eight. Counting the holes around the bolt circle in the high-angle view gives six, evenly spaced at 60 degrees. Set bolt_count to 8.',
    'All constraints met. The kernel measurements match the requested dimensions and every feature named in the prompt is present in the rendered views. The solid is watertight.',
)

_add(
    'manifold_block',
    'A hydraulic manifold block 120mm by 60mm by 50mm. A 16mm main gallery is drilled the full 120mm length. Three 10mm ports enter the top face on 40mm centres and meet the gallery. Four M10 mounting holes, 11mm clearance, sit 12mm in from each corner.',
    ['manifold', 'hydraulic', 'block', 'gallery', 'port', 'valve', 'cross-drilled', 'drilled'],
    ['solid block', 'main gallery', 'three top ports', 'four mounting holes'],
    {'length': 120, 'width': 60, 'height': 50, 'gallery_diameter': 16, 'port_diameter': 10, 'port_spacing': 40},
    "import cadquery as cq\n\nlength = 120.0\nwidth = 60.0\nheight = 50.0\ngallery_dia = 16.0\nport_dia = 10.0\nport_spacing = 40.0\nmount_hole_dia = 11.0\ncorner_inset = 12.0\n\n# Solid block\nresult = cq.Workplane('XY').box(\n    length, width, height, centered=(True, True, False)\n)\n\n# Main gallery along X, at mid height\ngallery = (\n    cq.Workplane('YZ')\n    .center(0, height / 2.0)\n    .circle(gallery_dia / 2.0)\n    .extrude(length, both=True)\n)\nresult = result.cut(gallery)\n\n# Ports entering the top face, meeting the gallery\nresult = (\n    result.faces('>Z').workplane(centerOption='CenterOfBoundBox')\n    .pushPoints([(-port_spacing, 0), (0, 0), (port_spacing, 0)])\n    .hole(port_dia, depth=height / 2.0)\n)\n\n# Corner mounting holes, full depth\nresult = (\n    result.faces('>Z').workplane(centerOption='CenterOfBoundBox')\n    .rect(length - 2 * corner_inset, width - 2 * corner_inset,\n          forConstruction=True)\n    .vertices()\n    .hole(mount_hole_dia)\n)\n",
    ('.hole(port_dia, depth=height / 2.0)', '.hole(port_dia, depth=8.0)'),
    'The top ports are only 8mm deep and stop well short of the gallery at mid height, so nothing connects. A manifold whose ports do not intersect the gallery passes no fluid. Drill them to at least height/2 so they break into the bore.',
    'All constraints met. The kernel measurements match the requested dimensions and every feature named in the prompt is present in the rendered views. The solid is watertight.',
)

_add(
    'motor_mount',
    'A NEMA 23 motor mounting plate: 100mm square, 8mm thick, with a 38.1mm central pilot bore and four 5.5mm holes on a 47.14mm square pattern. The four outer corners carry 6.5mm fixing holes 10mm in from each edge, and the corners are rounded to 8mm.',
    ['motor', 'nema', 'stepper', 'mount plate', 'mounting plate', 'pilot bore', 'servo'],
    ['square plate', 'rounded corners', 'pilot bore', 'NEMA bolt pattern', 'four fixing holes'],
    {'plate_size': 100, 'thickness': 8, 'pilot_bore': 38.1, 'nema_pattern': 47.14, 'nema_hole': 5.5},
    "import cadquery as cq\n\nplate_size = 100.0\nthickness = 8.0\npilot_bore_dia = 38.1\nnema_hole_dia = 5.5\nnema_pattern = 47.14\nfixing_hole_dia = 6.5\ncorner_inset = 10.0\ncorner_radius = 8.0\n\n# Plate with rounded corners\nresult = (\n    cq.Workplane('XY')\n    .box(plate_size, plate_size, thickness, centered=(True, True, False))\n    .edges('|Z')\n    .fillet(corner_radius)\n)\n\n# Central pilot bore\nresult = (\n    result.faces('>Z').workplane(centerOption='CenterOfBoundBox')\n    .circle(pilot_bore_dia / 2.0)\n    .cutThruAll()\n)\n\n# NEMA 23 bolt pattern\nresult = (\n    result.faces('>Z').workplane(centerOption='CenterOfBoundBox')\n    .rect(nema_pattern, nema_pattern, forConstruction=True)\n    .vertices()\n    .hole(nema_hole_dia)\n)\n\n# Outer fixing holes\nresult = (\n    result.faces('>Z').workplane(centerOption='CenterOfBoundBox')\n    .rect(plate_size - 2 * corner_inset, plate_size - 2 * corner_inset,\n          forConstruction=True)\n    .vertices()\n    .hole(fixing_hole_dia)\n)\n",
    ('pilot_bore_dia = 38.1', 'pilot_bore_dia = 22.0'),
    'The pilot bore measures 22mm but a NEMA 23 motor needs 38.1mm to clear its boss. The high-angle view shows a central hole far smaller than the bolt pattern around it. Set pilot_bore_dia to 38.1.',
    'All constraints met. The kernel measurements match the requested dimensions and every feature named in the prompt is present in the rendered views. The solid is watertight.',
)

_add(
    'v_pulley',
    'A V-belt pulley, 120mm outside diameter and 25mm wide, for an A section belt. The groove is 13mm wide at the rim, 11mm deep, with 34 degree included angle. A 30mm hub extends 15mm from one face and the bore is 20mm with a 6mm by 3mm keyway.',
    ['pulley', 'sheave', 'belt', 'v-belt', 'groove', 'drive', 'timing'],
    ['pulley body', 'V groove', 'hub', 'keyed bore'],
    {'outside_diameter': 120, 'face_width': 25, 'groove_depth': 11, 'bore': 20, 'hub_diameter': 30},
    "import cadquery as cq\nfrom math import tan, radians\n\noutside_dia = 120.0\nface_width = 25.0\ngroove_top_width = 13.0\ngroove_depth = 11.0\ngroove_angle = 34.0\nhub_dia = 30.0\nhub_length = 15.0\nbore_dia = 20.0\nkey_width = 6.0\nkey_depth = 3.0\n\n# Pulley body\nresult = cq.Workplane('XY').circle(outside_dia / 2.0).extrude(face_width)\n\n# Hub on the top face\nhub = (\n    cq.Workplane('XY').workplane(offset=face_width)\n    .circle(hub_dia / 2.0).extrude(hub_length)\n)\nresult = result.union(hub)\n\n# V groove, revolved from a trapezoid profile\nhalf_angle = radians(groove_angle / 2.0)\nbottom_half_width = groove_top_width / 2.0 - groove_depth * tan(half_angle)\nouter_radius = outside_dia / 2.0\ngroove = (\n    cq.Workplane('XZ')\n    .polyline([\n        (outer_radius + 1.0, face_width / 2.0 - groove_top_width / 2.0),\n        (outer_radius + 1.0, face_width / 2.0 + groove_top_width / 2.0),\n        (outer_radius - groove_depth, face_width / 2.0 + bottom_half_width),\n        (outer_radius - groove_depth, face_width / 2.0 - bottom_half_width),\n    ])\n    .close()\n    .revolve(360, (0, 0, 0), (0, 1, 0))\n)\nresult = result.cut(groove)\n\n# Bore through hub and body\nresult = (\n    result.faces('>Z').workplane(centerOption='CenterOfBoundBox')\n    .circle(bore_dia / 2.0)\n    .cutThruAll()\n)\n\n# Keyway\nkeyway = cq.Workplane('XY').box(\n    key_width, bore_dia / 2.0 + key_depth,\n    face_width + hub_length, centered=(True, False, False)\n)\nresult = result.cut(keyway)\n",
    ('result = result.cut(groove)', '# groove omitted'),
    'There is no groove in the rim. The front profile shows a plain cylindrical face, so no belt would ever seat. Revolve the trapezoidal profile about the pulley axis and cut it from the rim.',
    'All constraints met. The kernel measurements match the requested dimensions and every feature named in the prompt is present in the rendered views. The solid is watertight.',
)

_add(
    'cover_plate',
    'A round cover plate 140mm diameter and 10mm thick with an O-ring groove 4mm wide and 3mm deep on a 110mm mean diameter. Six 9mm bolt holes sit on a 125mm bolt circle, and a 20mm inspection hole is at the centre.',
    ['cover', 'lid', 'plate', 'o-ring', 'seal', 'gland', 'inspection', 'end cap'],
    ['round plate', 'O-ring groove', 'six bolt holes', 'inspection hole'],
    {'plate_diameter': 140, 'thickness': 10, 'groove_width': 4, 'groove_depth': 3, 'groove_mean_diameter': 110, 'bolt_count': 6},
    "import cadquery as cq\n\nplate_dia = 140.0\nthickness = 10.0\ngroove_width = 4.0\ngroove_depth = 3.0\ngroove_mean_dia = 110.0\nbolt_count = 6\nbolt_hole_dia = 9.0\nbolt_circle_dia = 125.0\ncentre_hole_dia = 20.0\n\n# Plate\nresult = cq.Workplane('XY').circle(plate_dia / 2.0).extrude(thickness)\n\n# O-ring groove in the top face\ngroove = (\n    cq.Workplane('XY').workplane(offset=thickness - groove_depth)\n    .circle((groove_mean_dia + groove_width) / 2.0)\n    .circle((groove_mean_dia - groove_width) / 2.0)\n    .extrude(groove_depth)\n)\nresult = result.cut(groove)\n\n# Central inspection hole\nresult = (\n    result.faces('>Z').workplane(centerOption='CenterOfBoundBox')\n    .circle(centre_hole_dia / 2.0)\n    .cutThruAll()\n)\n\n# Bolt circle\nresult = (\n    result.faces('>Z').workplane(centerOption='CenterOfBoundBox')\n    .polarArray(bolt_circle_dia / 2.0, 0, 360, bolt_count)\n    .hole(bolt_hole_dia)\n)\n",
    ('result = result.cut(groove)', '# groove omitted'),
    'The O-ring groove is absent. The top face reads as flat in the high-angle view, and the volume is higher than a grooved plate would be. Cut the annular groove 4mm wide and 3mm deep on the 110mm mean diameter.',
    'All constraints met. The kernel measurements match the requested dimensions and every feature named in the prompt is present in the rendered views. The solid is watertight.',
)

_add(
    'stepped_shaft',
    'A stepped transmission shaft 200mm long. A 25mm journal for 60mm, stepping up to a 35mm shoulder for 80mm, then down to a 20mm threaded end for 60mm. A 6mm wide by 3.5mm deep keyway runs 40mm along the journal, and both ends are chamfered 1.5mm.',
    ['shaft', 'stepped', 'journal', 'keyway', 'axle', 'spindle', 'transmission'],
    ['25mm journal', '35mm shoulder', '20mm threaded end', 'keyway', 'end chamfers'],
    {'journal_diameter': 25, 'journal_length': 60, 'shoulder_diameter': 35, 'shoulder_length': 80, 'end_diameter': 20, 'keyway_width': 6},
    "import cadquery as cq\n\njournal_dia = 25.0\njournal_length = 60.0\nshoulder_dia = 35.0\nshoulder_length = 80.0\nend_dia = 20.0\nend_length = 60.0\nkey_width = 6.0\nkey_depth = 3.5\nkey_length = 40.0\nchamfer_size = 1.5\n\n# Three diameters stacked along Z\nresult = cq.Workplane('XY').circle(journal_dia / 2.0).extrude(journal_length)\nresult = (\n    result.faces('>Z').workplane()\n    .circle(shoulder_dia / 2.0).extrude(shoulder_length)\n)\nresult = (\n    result.faces('>Z').workplane()\n    .circle(end_dia / 2.0).extrude(end_length)\n)\n\n# Keyway milled into the journal\nkeyway = cq.Workplane('XY').workplane(offset=10.0).box(\n    key_width, key_depth * 2.0, key_length, centered=(True, True, False)\n).translate((0, journal_dia / 2.0, 0))\nresult = result.cut(keyway)\n\n# Chamfer both ends\nresult = result.faces('>Z').chamfer(chamfer_size)\nresult = result.faces('<Z').chamfer(chamfer_size)\n",
    ('shoulder_dia = 35.0', 'shoulder_dia = 22.0'),
    'The middle section measures 22mm, smaller than the 25mm journal beside it, so the shaft steps down where it should step up. A shoulder must be larger than the journal it locates against. Set shoulder_dia to 35.0.',
    'All constraints met. The kernel measurements match the requested dimensions and every feature named in the prompt is present in the rendered views. The solid is watertight.',
)

_add(
    'flanged_bushing',
    'A flanged bronze bushing: 30mm outside diameter, 20mm bore, 40mm long, with a flange 42mm diameter and 5mm thick at one end. The bore has a 1mm chamfer at both ends.',
    ['bushing', 'bush', 'bearing sleeve', 'sleeve', 'liner', 'bronze', 'flanged'],
    ['flange', 'sleeve', 'bore', 'bore chamfers'],
    {'outer_diameter': 30, 'bore': 20, 'length': 40, 'flange_diameter': 42, 'flange_thickness': 5},
    "import cadquery as cq\n\nouter_dia = 30.0\nbore_dia = 20.0\nlength = 40.0\nflange_dia = 42.0\nflange_thickness = 5.0\nchamfer_size = 1.0\n\n# Flange at the bottom\nresult = cq.Workplane('XY').circle(flange_dia / 2.0).extrude(flange_thickness)\n\n# Sleeve above it\nsleeve = (\n    cq.Workplane('XY').workplane(offset=flange_thickness)\n    .circle(outer_dia / 2.0).extrude(length - flange_thickness)\n)\nresult = result.union(sleeve)\n\n# Bore through\nresult = (\n    result.faces('>Z').workplane(centerOption='CenterOfBoundBox')\n    .circle(bore_dia / 2.0)\n    .cutThruAll()\n)\n\n# Chamfer the bore at both ends\nresult = result.faces('>Z').edges(cq.selectors.RadiusNthSelector(0)).chamfer(chamfer_size)\nresult = result.faces('<Z').edges(cq.selectors.RadiusNthSelector(0)).chamfer(chamfer_size)\n",
    ('bore_dia = 20.0', 'bore_dia = 12.0'),
    'The bore is 12mm, not the 20mm specified. The high-angle view shows a wall far thicker than a bushing of this size should have. Set bore_dia to 20.0.',
    'All constraints met. The kernel measurements match the requested dimensions and every feature named in the prompt is present in the rendered views. The solid is watertight.',
)


_add(
    'flat_plate',
    'A rectangular plate 40mm long by 30mm wide by 10mm thick with a single 8mm hole through the centre.',
    ['plate', 'rectangular', 'flat', 'slab', 'rectangular plate', 'simple plate', 'blank'],
    ['rectangular plate', 'central through hole'],
    {'length': 40, 'width': 30, 'thickness': 10, 'hole_diameter': 8},
    "import cadquery as cq\n\nlength = 40.0\nwidth = 30.0\nthickness = 10.0\nhole_diameter = 8.0\n\n# Plate with a central through hole\nresult = (\n    cq.Workplane('XY')\n    .box(length, width, thickness, centered=(True, True, False))\n    .faces('>Z')\n    .hole(hole_diameter)\n)\n",
    ('thickness = 10.0', 'thickness = 20.0'),
    'The part measures 20.0mm in Z but the plan specifies a 10mm thickness, and the front profile view shows it standing far taller than a plate of this footprint should. Set thickness to 10.0.',
    'All constraints met. The bounding box is 40.0 x 30.0 x 10.0mm and the central bore is clearly visible in the high-angle view. The solid is watertight.',
)


def _tokens(text: str) -> list[str]:
    """Both sides of the match must be split the same way.

    "o-ring" and "l-shaped" become two tokens in a prompt, so a keyword
    holding the hyphen would never match one - which quietly sent
    "cover with an o-ring groove" to the pulley.
    """
    return re.findall(r"[a-z0-9]+", text.lower())


def select(prompt: str) -> MockPart:
    """Closest part to what was asked for.

    Deliberately forgiving: a vague request ("something to hold a rotating
    shaft") should still land on a real mechanical part rather than
    dead-ending, which is what a model would do.  A request matching
    nothing at all gets PARTS[0], the pillow block.
    """
    words = set(_tokens(prompt))
    best, best_score = PARTS[0], 0
    for part in PARTS:
        score = 0
        for keyword in part.keywords:
            tokens = _tokens(keyword)
            if tokens and all(token in words for token in tokens):
                # A longer phrase is a more specific match than a bare noun.
                score += 2 * len(tokens)
        if score > best_score:
            best, best_score = part, score
    return best
