#!/usr/bin/env python3
"""
stepdiff - tell me what actually changed between two STEP files.

v0 scope: cheap global invariants only (mass properties, bounding box,
topology counts). No B-rep face matching yet. This is deliberate: these
invariants are what engineers currently extract by hand into two Excel
sheets, so beating that workflow is the minimum viable win.

Exit codes:
  0  no differences beyond tolerance
  1  differences found
  2  error (bad file, unreadable, etc.)
"""

import argparse
import json
import sys
from dataclasses import dataclass, asdict

from OCP.STEPControl import STEPControl_Reader
from OCP.IFSelect import IFSelect_RetDone
from OCP.GProp import GProp_GProps
from OCP.BRepGProp import BRepGProp
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import (
    TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX, TopAbs_SHELL, TopAbs_SOLID,
)


@dataclass
class Metrics:
    volume: float
    area: float
    com_x: float
    com_y: float
    com_z: float
    bbox_dx: float
    bbox_dy: float
    bbox_dz: float
    bbox_xmin: float
    bbox_ymin: float
    bbox_zmin: float
    n_solids: int
    n_shells: int
    n_faces: int
    n_edges: int
    n_vertices: int


def read_step(path):
    reader = STEPControl_Reader()
    if reader.ReadFile(path) != IFSelect_RetDone:
        raise RuntimeError(f"could not read STEP file: {path}")
    reader.TransferRoots()
    shape = reader.OneShape()
    if shape.IsNull():
        raise RuntimeError(f"STEP file contained no usable shape: {path}")
    return shape


def count(shape, enum):
    exp = TopExp_Explorer(shape, enum)
    n = 0
    while exp.More():
        n += 1
        exp.Next()
    return n


def extract(shape) -> Metrics:
    vprops = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, vprops)
    sprops = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, sprops)
    com = vprops.CentreOfMass()

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()

    return Metrics(
        volume=vprops.Mass(),
        area=sprops.Mass(),
        com_x=com.X(), com_y=com.Y(), com_z=com.Z(),
        bbox_dx=xmax - xmin, bbox_dy=ymax - ymin, bbox_dz=zmax - zmin,
        bbox_xmin=xmin, bbox_ymin=ymin, bbox_zmin=zmin,
        n_solids=count(shape, TopAbs_SOLID),
        n_shells=count(shape, TopAbs_SHELL),
        n_faces=count(shape, TopAbs_FACE),
        n_edges=count(shape, TopAbs_EDGE),
        n_vertices=count(shape, TopAbs_VERTEX),
    )


# field -> (label, unit, kind)
FIELDS = [
    ("volume",     "Volume",           "mm^3",  "geom"),
    ("area",       "Surface area",     "mm^2",  "geom"),
    ("com_x",      "Centre of mass X", "mm",    "geom"),
    ("com_y",      "Centre of mass Y", "mm",    "geom"),
    ("com_z",      "Centre of mass Z", "mm",    "geom"),
    ("bbox_dx",    "Bounding box dX",  "mm",    "geom"),
    ("bbox_dy",    "Bounding box dY",  "mm",    "geom"),
    ("bbox_dz",    "Bounding box dZ",  "mm",    "geom"),
    ("bbox_xmin",  "Bounding box Xmin","mm",    "geom"),
    ("bbox_ymin",  "Bounding box Ymin","mm",    "geom"),
    ("bbox_zmin",  "Bounding box Zmin","mm",    "geom"),
    ("n_solids",   "Solids",           "",      "topo"),
    ("n_shells",   "Shells",           "",      "topo"),
    ("n_faces",    "Faces",            "",      "topo"),
    ("n_edges",    "Edges",            "",      "topo"),
    ("n_vertices", "Vertices",         "",      "topo"),
]


def compare(a: Metrics, b: Metrics, lin_tol: float, rel_tol: float):
    """Linear quantities use an absolute tolerance in mm.
    Volume/area use a relative tolerance, since abs mm^3 is meaningless.
    Topology counts must match exactly."""
    rows = []
    for key, label, unit, kind in FIELDS:
        va, vb = getattr(a, key), getattr(b, key)
        delta = vb - va

        if kind == "topo":
            changed = va != vb
            pct = None
        elif key in ("volume", "area"):
            pct = (delta / va * 100.0) if va else None
            changed = abs(pct) > rel_tol if pct is not None else delta != 0
        else:
            pct = (delta / va * 100.0) if va else None
            changed = abs(delta) > lin_tol

        rows.append({
            "field": key, "label": label, "unit": unit,
            "rev_a": va, "rev_b": vb, "delta": delta,
            "pct": pct, "changed": bool(changed),
        })
    return rows


def fmt(v, unit):
    if isinstance(v, int):
        return str(v)
    return f"{v:.4f}" if unit else f"{v:.4f}"


def render(rows, path_a, path_b, lin_tol, rel_tol):
    changed = [r for r in rows if r["changed"]]
    out = []
    out.append("")
    out.append(f"  rev A : {path_a}")
    out.append(f"  rev B : {path_b}")
    out.append(f"  tolerance: {lin_tol} mm linear / {rel_tol}% volumetric")
    out.append("")
    hdr = f"  {'':2}{'QUANTITY':<20}{'REV A':>14}{'REV B':>14}{'DELTA':>14}{'':>10}"
    out.append(hdr)
    out.append("  " + "-" * (len(hdr) - 2))
    for r in rows:
        mark = "!" if r["changed"] else " "
        pct = ""
        if r["pct"] is not None and r["changed"]:
            pct = f"{r['pct']:+.3f}%"
        out.append(
            f"  {mark:<2}{r['label']:<20}"
            f"{fmt(r['rev_a'], r['unit']):>14}"
            f"{fmt(r['rev_b'], r['unit']):>14}"
            f"{fmt(r['delta'], r['unit']):>14}"
            f"{pct:>10}"
        )
    out.append("")
    if changed:
        out.append(f"  {len(changed)} quantit{'y' if len(changed)==1 else 'ies'} changed beyond tolerance.")
    else:
        out.append("  No differences beyond tolerance.")
    out.append("")
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser(
        prog="stepdiff",
        description="Diff two STEP files. Nothing leaves your machine.",
    )
    p.add_argument("rev_a")
    p.add_argument("rev_b")
    p.add_argument("--tol", type=float, default=0.01,
                   help="linear tolerance in mm (default 0.01)")
    p.add_argument("--rel-tol", type=float, default=0.01,
                   help="volumetric tolerance in percent (default 0.01)")
    p.add_argument("--json", action="store_true", help="emit JSON")
    args = p.parse_args()

    try:
        a = extract(read_step(args.rev_a))
        b = extract(read_step(args.rev_b))
    except Exception as e:
        print(f"stepdiff: {e}", file=sys.stderr)
        return 2

    rows = compare(a, b, args.tol, args.rel_tol)
    any_changed = any(r["changed"] for r in rows)

    if args.json:
        print(json.dumps({
            "rev_a": {"path": args.rev_a, "metrics": asdict(a)},
            "rev_b": {"path": args.rev_b, "metrics": asdict(b)},
            "tolerance": {"linear_mm": args.tol, "relative_pct": args.rel_tol},
            "changed": any_changed,
            "rows": rows,
        }, indent=2))
    else:
        print(render(rows, args.rev_a, args.rev_b, args.tol, args.rel_tol))

    return 1 if any_changed else 0


if __name__ == "__main__":
    sys.exit(main())
