"""Fixed HERO-CAD box cross-check harness, executed only in the experiment sandbox."""

import json
import math
import sys

DIMENSIONS = (12.345, 23.456, 34.567)


def measure(shape):
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.GeomAbs import GeomAbs_Plane
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    if shape.IsNull() or not BRepCheck_Analyzer(shape).IsValid():
        return {"status": "invalid_solid"}
    solids = TopExp_Explorer(shape, TopAbs_SOLID)
    if not solids.More():
        return {"status": "invalid_solid"}
    solids.Next()
    if solids.More():
        return {"status": "unexpected_geometry"}

    mesh = BRepMesh_IncrementalMesh(shape, 0.01, False, 0.1, False)
    if not mesh.IsDone():
        return {"status": "measurement_unavailable"}
    faces = TopExp_Explorer(shape, TopAbs_FACE)
    contributions = []
    planes = set()
    corners = set()
    while faces.More():
        face = TopoDS.Face_s(faces.Current())
        faces.Next()
        if BRepAdaptor_Surface(face).GetType() != GeomAbs_Plane:
            return {"status": "unexpected_geometry"}
        location = TopLoc_Location()
        triangles = BRep_Tool.Triangulation_s(face, location)
        if triangles is None or triangles.NbTriangles() != 2 or triangles.NbNodes() != 4:
            return {"status": "measurement_unavailable"}
        points = [triangles.Node(i).Transformed(location.Transformation()).Coord() for i in range(1, 5)]
        labels = set()
        for point in points:
            label = []
            for x, side in zip(point, DIMENSIONS, strict=True):
                if abs(x) <= 1e-7:
                    label.append(0)
                elif abs(x - side) <= 1e-7:
                    label.append(1)
                else:
                    return {"status": "unexpected_geometry"}
            labels.add(tuple(label))
        corners.update(labels)
        constants = [(i, value) for i in range(3) for value in (0, 1)
                     if all(label[i] == value for label in labels)]
        if len(labels) != 4 or len(constants) != 1 or constants[0] in planes:
            return {"status": "unexpected_geometry"}
        planes.add(constants[0])
        for i in (1, 2):
            a, b, c = (points[k - 1] for k in triangles.Triangle(i).Get())
            det = (a[0] * (b[1]*c[2] - b[2]*c[1]) + a[1] * (b[2]*c[0] - b[0]*c[2])
                   + a[2] * (b[0]*c[1] - b[1]*c[0]))
            contributions.append((-det if face.Orientation() == TopAbs_REVERSED else det) / 6)
    if len(planes) != 6 or len(corners) != 8:
        return {"status": "unexpected_geometry"}
    return {"status": "measured", "mesh_mm3": math.fsum(contributions)}


def run():
    from hero_cad.document import BoxParameters, Document
    try:
        document = Document()
        document.set_box(BoxParameters(*DIMENSIONS))
    except Exception:
        return {"status": "regeneration_failed"}
    try:
        result = measure(document.shape)
    except Exception:
        return {"status": "measurement_unavailable"}
    if result["status"] == "measured":
        # Read the project's own observable only after the independent calculation.
        try:
            from hero_cad.geometry import volume
            result["reported_mm3"] = float(volume(document.shape))
            if not math.isfinite(result["reported_mm3"]):
                result["reported_mm3"] = None
        except Exception:
            result["reported_mm3"] = None
    return result


if __name__ == "__main__":
    if sys.version_info[:2] != (3, 12):
        raise SystemExit(78)
    sys.path.insert(0, "/runtime/packages")
    sys.path.insert(0, "/source/src")
    try:
        record = run()
    except (ImportError, OSError):
        raise SystemExit(78) from None
    print(json.dumps({"version": 1, **record}, allow_nan=False))
