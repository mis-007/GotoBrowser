package com.antest1.gotobrowser.Helpers;

import android.content.Context;
import android.util.Log;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import java.io.File;
import java.io.FileInputStream;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

/**
 * Ggotobrowser local-only ship illustration coordinate override.
 *
 * Reads only the [graph] section from external-files/coord_mod/*.config.ini.
 * Everything before [graph] (including [info], ship_name/getmes/sinfo and
 * non-UTF8 text) is ignored at byte level.
 */
public final class ShipGraphOverride {
    private static final String TAG = "GGOTO-COORD";
    private static final byte[] GRAPH_MARKER = "[graph]".getBytes(StandardCharsets.US_ASCII);

    private ShipGraphOverride() {}

    public static String apply(Context context, String responseRaw) {
        if (context == null || responseRaw == null || responseRaw.isEmpty()) return responseRaw;
        try {
            boolean hasPrefix = responseRaw.startsWith("svdata=");
            String json = hasPrefix ? responseRaw.substring(7) : responseRaw;
            JsonObject root = JsonParser.parseString(json).getAsJsonObject();
            JsonObject apiData = root.has("api_data") && root.get("api_data").isJsonObject()
                    ? root.getAsJsonObject("api_data") : null;
            if (apiData == null || !apiData.has("api_mst_shipgraph")
                    || !apiData.get("api_mst_shipgraph").isJsonArray()) return responseRaw;

            File external = context.getExternalFilesDir(null);
            if (external == null) return responseRaw;
            File coordDir = new File(external, "coord_mod");
            if (!coordDir.isDirectory()) return responseRaw;

            JsonArray graphs = apiData.getAsJsonArray("api_mst_shipgraph");
            int changed = 0;
            for (JsonElement element : graphs) {
                if (!element.isJsonObject()) continue;
                JsonObject graph = element.getAsJsonObject();
                String filename = safeString(graph, "api_filename");
                String shipId = safeString(graph, "api_id");
                File ini = findIni(coordDir, filename, shipId);
                if (ini == null) continue;
                Map<String, Integer> values = parseGraphOnly(ini);
                if (values.isEmpty()) continue;
                if (applyGraphValues(graph, values)) {
                    changed++;
                    Log.i(TAG, "Applied: " + ini.getName() + " -> " + filename + " / " + shipId);
                }
            }
            if (changed > 0) Log.i(TAG, "Overridden shipgraph entries: " + changed);
            String result = root.toString();
            return hasPrefix ? "svdata=" + result : result;
        } catch (Throwable t) {
            // Fail open: malformed INI/JSON must never block the game.
            Log.e(TAG, "Coordinate override failed; using official data", t);
            return responseRaw;
        }
    }

    private static String safeString(JsonObject obj, String key) {
        try {
            if (!obj.has(key) || obj.get(key).isJsonNull()) return "";
            return obj.get(key).getAsString();
        } catch (Throwable ignored) {
            return "";
        }
    }

    private static File findIni(File dir, String filename, String shipId) {
        String[] names = new String[] {
                filename.isEmpty() ? "" : filename + ".config.ini",
                filename.isEmpty() ? "" : filename + "_1.config.ini",
                shipId.isEmpty() ? "" : shipId + ".config.ini"
        };
        for (String name : names) {
            if (name.isEmpty()) continue;
            File f = new File(dir, name);
            if (f.isFile() && f.length() > 0) return f;
        }
        // Compatibility fallback for ShimakazeGo packs that append a suffix.
        File[] files = dir.listFiles();
        if (files != null && !filename.isEmpty()) {
            String prefix = filename.toLowerCase(Locale.ROOT);
            for (File f : files) {
                String n = f.getName().toLowerCase(Locale.ROOT);
                if (f.isFile() && n.startsWith(prefix) && n.endsWith(".config.ini")) return f;
            }
        }
        return null;
    }

    /**
     * Locate ASCII [graph] directly in bytes, then parse only ASCII keys/numbers.
     * This makes [info] encoding irrelevant (UTF-8/GBK/Shift-JIS/ANSI can coexist).
     */
    static Map<String, Integer> parseGraphOnly(File file) throws Exception {
        byte[] raw;
        try (FileInputStream in = new FileInputStream(file)) {
            raw = new byte[(int) file.length()];
            int off = 0;
            while (off < raw.length) {
                int n = in.read(raw, off, raw.length - off);
                if (n < 0) break;
                off += n;
            }
        }
        int marker = indexOfIgnoreAsciiCase(raw, GRAPH_MARKER);
        Map<String, Integer> out = new LinkedHashMap<>();
        if (marker < 0) return out;
        int start = marker + GRAPH_MARKER.length;
        String graphText = new String(raw, start, raw.length - start, StandardCharsets.ISO_8859_1);
        String[] lines = graphText.replace("\r", "").split("\n");
        for (String rawLine : lines) {
            String line = rawLine.trim();
            if (line.isEmpty() || line.startsWith(";") || line.startsWith("#")) continue;
            if (line.startsWith("[") && line.endsWith("]")) break;
            int eq = line.indexOf('=');
            if (eq <= 0) continue;
            String key = line.substring(0, eq).trim().toLowerCase(Locale.ROOT);
            String value = line.substring(eq + 1).trim();
            int comment = value.indexOf(';');
            if (comment >= 0) value = value.substring(0, comment).trim();
            try {
                out.put(key, Integer.parseInt(value));
            } catch (NumberFormatException ignored) {
                // Invalid values are ignored individually.
            }
        }
        return out;
    }

    private static boolean applyGraphValues(JsonObject graph, Map<String, Integer> values) {
        // Any <name>_left / <name>_top pair maps to api_<name> = [left, top].
        boolean changed = false;
        for (String key : values.keySet()) {
            String suffix;
            if (key.endsWith("_left")) suffix = "_left";
            else if (key.endsWith("_top")) suffix = "_top";
            else continue;
            String base = key.substring(0, key.length() - suffix.length());
            String apiKey = base.startsWith("api_") ? base : "api_" + base;
            if (!graph.has(apiKey) || !graph.get(apiKey).isJsonArray()) continue;
            JsonArray old = graph.getAsJsonArray(apiKey);
            if (old.size() < 2) continue;
            int left = values.containsKey(base + "_left")
                    ? values.get(base + "_left") : safeInt(old.get(0));
            int top = values.containsKey(base + "_top")
                    ? values.get(base + "_top") : safeInt(old.get(1));
            JsonArray replacement = new JsonArray();
            replacement.add(left);
            replacement.add(top);
            // Preserve any extra elements if the upstream format ever grows.
            for (int i = 2; i < old.size(); i++) replacement.add(old.get(i));
            graph.add(apiKey, replacement);
            changed = true;
        }
        return changed;
    }

    private static int safeInt(JsonElement e) {
        try { return e.getAsInt(); } catch (Throwable ignored) { return 0; }
    }

    private static int indexOfIgnoreAsciiCase(byte[] data, byte[] needle) {
        if (needle.length == 0) return 0;
        outer:
        for (int i = 0; i <= data.length - needle.length; i++) {
            for (int j = 0; j < needle.length; j++) {
                int a = data[i + j] & 0xff;
                int b = needle[j] & 0xff;
                if (a >= 'A' && a <= 'Z') a += 32;
                if (b >= 'A' && b <= 'Z') b += 32;
                if (a != b) continue outer;
            }
            return i;
        }
        return -1;
    }
}
