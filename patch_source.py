#!/usr/bin/env python3
"""GGoto source-level mod: no network, no silent fallback, no changes to package or broadcast.

Usage: python patch_source.py /path/to/GotoBrowser
"""
from pathlib import Path
import re
import shutil
import sys

HERE = Path(__file__).resolve().parent


class PatchError(Exception):
    pass


def exact_replace(source: str, needle: str, replacement: str, name: str) -> str:
    count = source.count(needle)
    if count != 1:
        raise PatchError(f"{name}: expected one anchor, got {count}. Upstream changed; refusing unsafe patch.")
    return source.replace(needle, replacement, 1)


def patch(root: Path, report=print):
    root = Path(root).resolve()
    rel_b = Path('app/src/main/java/com/antest1/gotobrowser/Browser')
    rel_h = Path('app/src/main/java/com/antest1/gotobrowser/Helpers')
    kcs_path = root / rel_b / 'KcsInterface.java'
    res_path = root / rel_b / 'ResourceProcess.java'
    helper_path = root / rel_h / 'ShipGraphOverride.java'
    if not kcs_path.is_file() or not res_path.is_file():
        raise PatchError('Cannot find KcsInterface.java or ResourceProcess.java. Not matching GotoBrowser source.')
    if helper_path.exists() or 'gg_shipgraph_override' in kcs_path.read_text(encoding='utf-8'):
        raise PatchError('This source appears already patched. Use a clean official source checkout.')
    kcs = kcs_path.read_text(encoding='utf-8')
    rp = res_path.read_text(encoding='utf-8')
    gradle_path = root / 'app/build.gradle'
    gradle = gradle_path.read_text(encoding='utf-8')
    if 'applicationId "com.antest1.gotobrowser"' not in gradle and "applicationId 'com.antest1.gotobrowser'" not in gradle:
        raise PatchError('applicationId differs; cannot guarantee Kcanotify integration. Refusing to patch.')

    # Coordinate changes ONLY after Kcanotify's original packet is passed to its JS bridge.
    import_line = 'import com.antest1.gotobrowser.Helpers.ShipGraphOverride;'
    if import_line not in kcs:
        m = re.search(r'^package\s+[^;]+;', kcs, flags=re.M)
        if not m:
            raise PatchError('Cannot find package declaration in KcsInterface.java')
        kcs = kcs[:m.end()] + '\n' + import_line + kcs[m.end():]

    annotation = re.search(r'(?m)^\s*@JavascriptInterface\s*\n\s*public\s+void\s+kcs_xhr_intercept\s*\(', kcs)
    if not annotation:
        raise PatchError('Cannot find @JavascriptInterface kcs_xhr_intercept for injecting bridge.')
    bridge = '''    /** GGoto local visual-only override. Never changes packets broadcast to Kcanotify. */
    @JavascriptInterface
    public String gg_shipgraph_override(String url, String responseRaw) {
        if (url == null || responseRaw == null || !url.contains("api_start2")) return responseRaw;
        try {
            return ShipGraphOverride.apply(activity.getApplicationContext(), responseRaw);
        } catch (Throwable failure) {
            android.util.Log.e("GGOTO-COORD", "INI override failed; retain game data", failure);
            return responseRaw;
        }
    }

'''
    kcs = kcs[:annotation.start()] + '\n' + bridge + kcs[annotation.start():]
    interceptor = 'GotoBrowser.kcs_xhr_intercept(host,url,request,data);'
    # A single-quoted JS literal is safe within Java's double-quoted string.
    # For safety preserve existing return and all Kcanotify interception logic.
    inject_js = "if(url.indexOf('api_start2')>=0){response.data=GotoBrowser.gg_shipgraph_override(url,response.data);}"
    kcs = exact_replace(kcs, interceptor, interceptor + inject_js, 'Axios interception')
    if 'AXIOS_INTERCEPT_SCRIPT' not in kcs or 'return response;' not in kcs:
        raise PatchError('Unexpected Axios response structure; refusing patch.')

    # The original cache is downloaded/updated exactly as before. Hack is chosen only for final display.
    if '.hack.png' in rp and 'GGOTO-HACK' in rp:
        raise PatchError('ResourceProcess is already patched.')
    method = re.search(r'(?m)^\s*private\s+WebResourceResponse\s+processImageDataResource\s*\(', rp)
    if not method:
        raise PatchError('Cannot find processImageDataResource method; upstream changed.')
    helper = '''    /** Return a local custom image, never the official cache download destination. */
    private java.io.File ggFindHackImage(String officialFile) {
        if (officialFile == null || officialFile.length() == 0) return null;
        int cut = officialFile.lastIndexOf('.');
        int slash = Math.max(officialFile.lastIndexOf('/'), officialFile.lastIndexOf('\\\\'));
        if (cut <= slash) return null;
        String prefix = officialFile.substring(0, cut);
        java.io.File custom = new java.io.File(prefix + ".hack.png");
        if (custom.isFile() && custom.length() > 0) return custom;
        return null;
    }

'''
    rp = rp[:method.start()] + '\n' + helper + rp[method.start():]
    method_start = rp.index('processImageDataResource(')
    # Search for the target after the method declaration, not a call site.
    method_start = rp.index('processImageDataResource(', rp.index('ggFindHackImage('))
    next_method = re.search(r'(?m)^\s*(?:private|public|protected)\s+(?:static\s+)?[\w<>\[\]]+\s+\w+\s*\(', rp[method_start+1:])
    method_end = method_start + 1 + next_method.start() if next_method else len(rp)
    block = rp[method_start:method_end]
    stream_pat = re.compile(r'(?m)^\s*InputStream\s+\w+\s*=\s*new\s+BufferedInputStream\s*\(\s*new\s+FileInputStream\s*\(\s*file\s*\)\s*\)\s*;')
    stream_matches = list(stream_pat.finditer(block))
    if len(stream_matches) != 1 or 'out_file_path' not in block or 'resource_type' not in block:
        raise PatchError('Cannot find unique final FileInputStream(file) inside image handler; refusing unsafe patch.')
    insert_at = method_start + stream_matches[0].start()
    image_hook = '''
            // GGOTO-HACK: keep official cache/download path untouched; custom file is display-only.
            if (ResourceProcess.isImage(resource_type)) {
                java.io.File ggFile = ggFindHackImage(out_file_path);
                if (ggFile != null) {
                    file = ggFile;
                    log_path = ggFile.getAbsolutePath();
                    android.util.Log.i("GGOTO-HACK", "Custom image: " + log_path);
                }
            }
'''
    rp = rp[:insert_at] + image_hook + rp[insert_at:]

    # Remove Firebase from the experimental debug build AS A SET. Otherwise startup may crash.
    gradle = re.sub(r'(?m)^\s*apply\s+plugin:\s*[\'\"]com\.google\.(?:gms\.google-services|firebase\.crashlytics)[\'\"]\s*\n', '', gradle)
    gradle = re.sub(r'(?m)^\s*(?:implementation|api)\s+\(?\s*[\'\"]com\.google\.firebase:[^\n]+\n', '', gradle)
    if 'com.google.gms.google-services' in gradle or 'com.google.firebase.crashlytics' in gradle or 'com.google.firebase:' in gradle:
        raise PatchError('Firebase build references changed; cannot safely remove.')
    # Replace Crashlytics-specific references in source only; don't remove other exception handling.
    java_changes = {}
    for path in (root / 'app/src/main/java').rglob('*.java'):
        txt = path.read_text(encoding='utf-8')
        if 'FirebaseCrashlytics' not in txt and not re.search(r'\bcrashlytics\.', txt):
            continue
        modified = re.sub(r'(?m)^\s*import\s+com\.google\.firebase\.crashlytics\.FirebaseCrashlytics;\s*\n', '', txt)
        modified = re.sub(r'(?m)^\s*(?:private|public|protected)?\s*(?:static\s+)?(?:final\s+)?FirebaseCrashlytics\s+crashlytics\s*=\s*FirebaseCrashlytics\.getInstance\(\);\s*\n', '', modified)
        modified = re.sub(r'(?m)^\s*crashlytics\.(?:recordException|log|setCustomKey|setUserId)\([^;]*\);\s*\n', '', modified)
        if 'FirebaseCrashlytics' in modified or re.search(r'\bcrashlytics\.', modified):
            raise PatchError(f'Unhandled Crashlytics Java reference in {path.relative_to(root)}')
        java_changes[path] = modified
    if not java_changes:
        report('Firebase Java references not present in source; continuing.')

    strings = root / 'app/src/main/res/values/strings.xml'
    strings_text = strings.read_text(encoding='utf-8') if strings.is_file() else None
    if strings_text:
        strings_modified, count = re.subn(r'(<string\s+name="app_name"[^>]*>)(.*?)(</string>)', r'\1Ggotobrowser\3', strings_text, count=1, flags=re.S)
        if count != 1:
            report('Warning: could not find app_name string; package remains unchanged.')
            strings_modified = strings_text
    else:
        strings_modified = None

    # Atomic enough: stage text changes before writing to avoid leaving half-patched source if anchor fails.
    kcs_path.write_text(kcs, encoding='utf-8')
    res_path.write_text(rp, encoding='utf-8')
    shutil.copy2(HERE / 'ShipGraphOverride.java', helper_path)
    gradle_path.write_text(gradle, encoding='utf-8')
    for p, t in java_changes.items():
        p.write_text(t, encoding='utf-8')
    if strings_modified is not None:
        strings.write_text(strings_modified, encoding='utf-8')

    checks = {
        'local coordinate bridge': 'gg_shipgraph_override(url,response.data)' in kcs,
        'original packet broadcast': 'GotoBrowser.kcs_xhr_intercept(host,url,request,data)' in kcs,
        'hack overlay': 'GGOTO-HACK' in rp and 'ggFindHackImage' in rp,
        'INI parser': helper_path.is_file(),
        'original package name': 'com.antest1.gotobrowser' in gradle,
    }
    for label, valid in checks.items():
        report(f'{label}: {"PASS" if valid else "FAIL"}')
    if not all(checks.values()):
        raise PatchError('Post-patch verification failed.')
    report('GGoto mod applied to SOURCE. APK compilation and device testing are still required.')


if __name__ == '__main__':
    try:
        patch(Path(sys.argv[1] if len(sys.argv) > 1 else '.'))
    except (PatchError, OSError) as exc:
        print('GGOTO PATCH FAILED:', exc, file=sys.stderr)
        sys.exit(2)
