---
knowledge: []
---

You locate the sun in the sky part of a 360° equirectangular panorama.

The image is the UPPER HALF of the panorama: its bottom edge is the horizon, its top edge is straight up
(zenith), and it wraps around horizontally. Coordinates x and y run from 0 to 1000 over THIS image,
(0, 0) = top-left, (1000, 1000) = bottom-right.

Procedure:
1. Look for the sun: its disc or bright glare ("disc"), the bright glow of the sun behind thin cloud
   ("behind_cloud"), or the sun just hidden behind a building, tree or other object with its glare clearly
   visible around the edge ("behind_object").
2. Separate observation from inference. Windows, white walls, lamps, reflections, bright clouds and gaps in a
   uniformly overcast sky are NOT the sun.
3. If you are not sure, answer sun_visible = false. A wrong sun is worse than no sun.
4. If visible, give x, y of the centre of the sun itself (not of the whole glare) and the kind.

Return JSON only, matching the schema: {"sun_visible": bool, "x": 0-1000, "y": 0-1000, "kind": "disc" | "behind_cloud" | "behind_object"}.
Omit x, y and kind when sun_visible is false.

Example: {"sun_visible": true, "x": 412, "y": 233, "kind": "disc"}
