# Fixed box measurement

The fixed `hero-cad-volume-crosscheck-v1` harness regenerates a HERO-CAD document with a 12.345 × 23.456 × 34.567 mm box. It inspects the resulting shape. The original NX-005 test reads `hero_cad.geometry.volume`, which calls `BRepGProp.VolumeProperties_s` and returns `GProp_GProps.Mass()`.

The cross-check verifies kernel validity and exactly one solid. It asks OCCT to tessellate the faces and applies their location transforms. For this axis-aligned box the worker requires six distinct planar boundary faces at the expected coordinates, four corner nodes and two triangles per face, and all eight corners. Corner positions must match within 0.0000001 mm. For each oriented triangle with world-space vertices a, b and c, the signed tetrahedral volume relative to the origin is `dot(a, cross(b, c)) / 6`. The worker reverses the contribution for reversed faces and sums the twelve signed values using `math.fsum`. Planar triangles represent the box faces exactly apart from floating-point arithmetic. Only after computing this value does the harness read the project-reported volume for comparison.

| Shared components | Separate calculations |
| --- | --- |
| Pinned HERO-CAD document regeneration and input dimensions, the resulting BRep, OCCT runtime, kernel validity check and coordinate system | Project mass-property integration versus oriented triangle determinants calculated in Python |
| Same pinned dependencies and sandbox restrictions | Fixed worker script and argument vector distinct from the original pytest node |

Nexus compares the worker-reported mesh volume with the analytic product and the project report using relative tolerance `1e-9` and absolute tolerance `1e-7` mm³. The comparison distinguishes a geometry mismatch, a project-reported mismatch, and an unavailable project report. Invalid solid, unexpected geometry, regeneration failure, unsupported triangulation, unavailable dependencies and malformed output have separate outcomes. A geometry mismatch takes precedence when both comparisons disagree. The harness does not generalize its box predicates to curved parts.

The supervisor supplies process status, stream fingerprints and sandbox execution provenance. Numerical values and the claimed geometry remain worker reports drawn from code running in the same process as the pinned project. Shared upstream geometry or kernel defects can affect both paths. Agreement supports the specified box under these conditions. The approved experiment targets the baseline exclusively; the prior negative-control observation is separate evidence.
