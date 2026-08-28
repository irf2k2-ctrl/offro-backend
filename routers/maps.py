*** Begin Patch
*** Update File: routers/maps.py
@@
     lat, lng = _extract_coordinates(url)

     resolved_url = url

     place_name = _extract_place_name(url)

     address = ""

     place_id = _extract_place_id(url)
+
+    # Short Google Maps share links are special:
+    # Google can redirect them to a URL containing the server's/default
+    # map viewport coordinates, which are NOT necessarily the merchant's
+    # actual place coordinates. Never trust redirected/HTML coordinates
+    # for maps.app.goo.gl or goo.gl when the original URL has no coordinates.
+    try:
+        original_host = urlparse(url).netloc.lower().split(":")[0]
+    except Exception:
+        original_host = ""
+
+    is_short_maps_link = original_host in {
+        "maps.app.goo.gl",
+        "goo.gl",
+    }
@@
-        if lat is None or lng is None:
+        if (lat is None or lng is None) and not is_short_maps_link:

             lat, lng = _extract_coordinates(
                 resolved_url
             )
@@
-            if lat is None or lng is None:
+            if (lat is None or lng is None) and not is_short_maps_link:

                 lat, lng = (
                     _extract_coordinates_from_html(
                         html
                     )
*** End Patch
