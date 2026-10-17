import React, { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";

// KiCad's GLB is in metres, Y up; board X/Y (mm) map to GLB X/Z.
const MM = 0.001;
const FLASH_MS = 6000;
const COLORS = { picked: 0x6aa8ff, bad: 0xf85149, added: 0x3fb950, swapped: 0xd29922, hover: 0x3b82f6 };

export default function Board3D({ runId, designVersion, failing = [] }) {
  const mount = useRef(null);
  const ctx = useRef(null);
  const [status, setStatus] = useState("Building board in KiCad…");
  const [busy, setBusy] = useState(false);          // a newer board is being built; old one stays visible
  const [info, setInfo] = useState(null);
  const [picked, setPicked] = useState(null);
  const [hover, setHover] = useState(null);
  const [flash, setFlash] = useState({});           // ref -> "added" | "swapped"
  const [note, setNote] = useState(null);
  const [showParts, setShowParts] = useState(true);
  const [showMask, setShowMask] = useState(true);   // off = bare copper, to see the wiring
  const [tick, setTick] = useState(0);              // bumps when a new GLB is in the scene

  // Scene setup once.
  useEffect(() => {
    const el = mount.current;
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(window.devicePixelRatio);
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    el.appendChild(renderer.domElement);
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(getComputedStyle(document.documentElement).getPropertyValue('--panel-2').trim() || 0x0d1117);
    const camera = new THREE.PerspectiveCamera(35, 1, 0.0005, 5);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    scene.add(new THREE.HemisphereLight(0xffffff, 0x334155, 1.6));
    const sun = new THREE.DirectionalLight(0xffffff, 2.2);
    sun.position.set(0.05, 0.12, 0.08);
    scene.add(sun);
    const fill = new THREE.DirectionalLight(0x9ec5ff, 0.6);
    fill.position.set(-0.08, 0.05, -0.06);
    scene.add(fill);
    const grid = new THREE.GridHelper(0.3, 60, 0x1f2a37, 0x161d27);
    grid.position.y = -0.0005;
    scene.add(grid);

    const resize = () => {
      const w = el.clientWidth, h = el.clientHeight;
      renderer.setSize(w, h);
      camera.aspect = w / Math.max(h, 1);
      camera.updateProjectionMatrix();
    };
    const ro = new ResizeObserver(resize);
    ro.observe(el);
    resize();

    let raf;
    const loop = () => {
      controls.update();
      // Pulse flashing parts.
      const c = ctx.current;
      if (c?.pulse?.length) {
        const k = 0.35 + 0.35 * Math.sin(performance.now() / 160);
        c.pulse.forEach((m) => { if (m.material.emissive) m.material.emissiveIntensity = k; });
      }
      renderer.render(scene, camera);
      raf = requestAnimationFrame(loop);
    };
    loop();

    const ray = new THREE.Raycaster();
    const ndc = new THREE.Vector2();
    const refAt = (ev) => {
      const r = renderer.domElement.getBoundingClientRect();
      ndc.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
      ray.setFromCamera(ndc, camera);
      const root = ctx.current?.root;
      if (!root) return null;
      for (const hit of ray.intersectObject(root, true)) {
        if (!hit.object.visible) continue;
        let o = hit.object;
        while (o && !o.userData.ref) o = o.parent;
        if (o && o.visible) return o.userData.ref;
      }
      return null;
    };
    let down = null;
    const onDown = (ev) => { down = [ev.clientX, ev.clientY]; };
    const onUp = (ev) => {
      if (down && Math.hypot(ev.clientX - down[0], ev.clientY - down[1]) < 4) setPicked(refAt(ev));
      down = null;
    };
    const onMove = (ev) => setHover(refAt(ev));
    renderer.domElement.addEventListener("pointerdown", onDown);
    renderer.domElement.addEventListener("pointerup", onUp);
    renderer.domElement.addEventListener("pointermove", onMove);

    ctx.current = { renderer, scene, camera, controls, root: null, parts: {}, framed: null, glbUrl: null, pulse: [] };
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      controls.dispose();
      renderer.dispose();
      el.removeChild(renderer.domElement);
    };
  }, []);

  const loadGlb = (meta) => new Promise((resolve, reject) => {
    const c = ctx.current;
    if (c.glbUrl === meta.glb_url) return resolve(false);
    new GLTFLoader().load(meta.glb_url, (gltf) => {
      const root = gltf.scene;
      const parts = {};
      root.traverse((o) => { if (meta.placements[o.name]) { o.userData.ref = o.name; parts[o.name] = o; } });
      // Board layer meshes are unnamed; KiCad's soldermask is the green-dominant material.
      const masks = [];
      root.traverse((o) => {
        if (!o.isMesh) return;
        let p = o; while (p && !p.userData.ref) p = p.parent;
        const col = o.material?.color;
        if (!p && col && col.g > col.r * 1.15 && col.g > col.b * 1.15) masks.push(o);
      });
      // Parts without a KiCad 3D model get a plain block so nothing is invisible.
      const top = (meta.thickness_mm || 1.6) * MM;
      for (const [ref, p] of Object.entries(meta.placements)) {
        if (parts[ref]) continue;
        const hgt = Math.min(Math.max(Math.min(p.w, p.h) * 0.25, 0.8), 2.5) * MM;
        const box = new THREE.Mesh(new THREE.BoxGeometry(p.w * 0.9 * MM, hgt, p.h * 0.9 * MM),
          new THREE.MeshStandardMaterial({ color: 0x2b2f36, roughness: 0.6, metalness: 0.1 }));
        box.position.set(p.x * MM, top + hgt / 2, p.y * MM);
        box.name = ref;
        box.userData.ref = ref;
        root.add(box);
        parts[ref] = box;
      }
      if (c.root) c.scene.remove(c.root);
      c.scene.add(root);
      c.root = root;
      c.parts = parts;
      c.glbUrl = meta.glb_url;
      c.masks = masks;
      // Frame only the first time (or when the board size changes); otherwise keep the user's view.
      const size = `${meta.width_mm}x${meta.height_mm}`;
      if (c.framed !== size) {
        const w = meta.width_mm * MM, h = meta.height_mm * MM;
        const center = new THREE.Vector3(w / 2, 0, h / 2);
        const fov = (c.camera.fov * Math.PI) / 180;
        const span = Math.max(w / Math.max(c.camera.aspect, 0.5), h) * 0.5;
        const dist = (span / Math.tan(fov / 2)) * 1.35;
        if (c.framed === null) {
          c.controls.target.copy(center);
          c.camera.position.set(center.x, dist * 0.82, center.z + dist * 0.57);
        } else {
          c.controls.target.copy(center);
        }
        c.framed = size;
      }
      setTick((t) => t + 1);
      resolve(true);
    }, undefined, reject);
  });

  // Fetch board info for the current design; poll while routing so the routed board swaps in.
  useEffect(() => {
    if (!runId) return;
    let cancelled = false, timer = null;
    setBusy(true);
    const poll = (first) => {
      fetch(`/api/runs/${runId}/board.json`)
        .then((r) => (r.ok ? r.json() : r.text().then((t) => Promise.reject(new Error(t)))))
        .then(async (meta) => {
          if (cancelled) return;
          const fresh = await loadGlb(meta);
          if (cancelled) return;
          setInfo(meta);
          setStatus(null);
          setBusy(false);
          if (first && fresh) {
            const ch = meta.changes || {};
            const f = {};
            (ch.added || []).forEach((r) => { f[r] = "added"; });
            (ch.swapped || []).forEach((s) => { f[s.ref] = "swapped"; });
            if (!ch.first && Object.keys(f).length + (ch.removed || []).length) {
              setFlash(f);
              const bits = [];
              if (ch.added?.length) bits.push(`+ ${ch.added.join(", ")}`);
              (ch.swapped || []).forEach((s) => bits.push(`${s.ref}: ${s.from} → ${s.to}`));
              if (ch.removed?.length) bits.push(`− ${ch.removed.join(", ")}`);
              setNote(bits.join(" · "));
              setTimeout(() => { setFlash({}); setNote(null); }, FLASH_MS);
            }
          }
          if (meta.route?.status === "routing") timer = setTimeout(() => poll(false), 2000);
        })
        .catch((e) => {
          if (cancelled) return;
          setBusy(false);
          setStatus(e.message.includes("KiCad") ? e.message : `Board build failed: ${e.message.slice(0, 300)}`);
        });
    };
    poll(true);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [runId, designVersion]);

  // Visibility and tints: failing, changed, hovered and picked parts.
  useEffect(() => {
    const c = ctx.current;
    if (!c?.root) return;
    const bad = new Set(failing);
    c.pulse = [];
    (c.masks || []).forEach((m) => { m.visible = showMask; });
    for (const [ref, o] of Object.entries(c.parts)) {
      o.visible = showParts;
      const kind = ref === picked ? "picked" : bad.has(ref) ? "bad" : flash[ref] || (ref === hover ? "hover" : null);
      o.traverse((m) => {
        if (!m.isMesh) return;
        if (!m.userData.baseMat) m.userData.baseMat = m.material;
        if (!kind) { m.material = m.userData.baseMat; return; }
        const mat = m.userData.baseMat.clone();
        if (mat.emissive) {
          mat.emissive.setHex(COLORS[kind]);
          mat.emissiveIntensity = kind === "hover" ? 0.35 : 0.6;
        }
        m.material = mat;
        if (kind === "added" || kind === "swapped") c.pulse.push(m);
      });
    }
    c.renderer.domElement.style.cursor = hover ? "pointer" : "grab";
  }, [failing, hover, picked, flash, showParts, showMask, tick]);

  const p = picked && info?.placements?.[picked];
  const r = info?.route;
  const drc = r?.drc;
  const drcErrors = drc ? Object.values(drc.errors || {}).reduce((a, b) => a + b, 0) : null;
  return (
    <div className="board3d">
      <div ref={mount} className="board3d-canvas" />
      {status && <div className="board3d-status">{status}</div>}
      {info && !status && (
        <div className="board3d-meta">
          <div>
            {info.width_mm} × {info.height_mm} mm · {info.footprints} parts ·{" "}
            {r?.status === "routed" ? <span className="ok">routed: {r.tracks} tracks, {r.vias} vias{r.unrouted?.length ? `, ${r.unrouted.length} unrouted` : ""}</span>
              : r?.status === "routing" ? <span className="warn pulse">routing with Freerouting…</span>
              : <span className="muted">not routed ({r?.error || r?.status})</span>}
          </div>
          {drc && (
            <div className={drcErrors || drc.unconnected ? "bad small" : "ok small"}>
              KiCad DRC: {drcErrors} error{drcErrors === 1 ? "" : "s"}, {drc.unconnected} unconnected
              {Object.keys(drc.library || {}).length > 0 && <span className="muted"> · {Object.values(drc.library).reduce((a, b) => a + b, 0)} library-footprint notes</span>}
            </div>
          )}
          {info.models_missing?.length > 0 && <div className="muted small">{info.models_missing.length} parts have no KiCad 3D model (grey blocks)</div>}
          <label className="small"><input type="checkbox" checked={showParts} onChange={(e) => setShowParts(e.target.checked)} /> components</label>{" "}
          <label className="small"><input type="checkbox" checked={showMask} onChange={(e) => setShowMask(e.target.checked)} /> solder mask</label>
          <span className="muted small"> · drag to orbit · scroll to zoom · right-drag to pan · click a part</span>
        </div>
      )}
      {busy && info && <div className="board3d-busy">updating board…</div>}
      {note && <div className="board3d-note">{note}</div>}
      {hover && hover !== picked && <div className="board3d-hover">{hover} · {info?.placements?.[hover]?.mpn}</div>}
      {p && (
        <div className="board3d-card">
          <button className="x" onClick={() => setPicked(null)}>×</button>
          <h3>{picked} <small>{p.subsystem}</small></h3>
          <div><code>{p.mpn}</code> {p.value && p.value !== p.mpn ? `· ${p.value}` : ""}</div>
          <div className="muted small">{p.description}</div>
          <div className="muted small">{p.footprint}</div>
          {Object.keys(p.nets || {}).length > 0 && (
            <table className="opts"><tbody>
              {Object.entries(p.nets).sort((a, b) => a[0].localeCompare(b[0], undefined, { numeric: true }))
                .map(([pin, net]) => <tr key={pin}><td className="muted">pin {pin}</td><td>{net}</td></tr>)}
            </tbody></table>
          )}
          {failing.includes(picked) && <div className="bad small">named in a failing verifier result</div>}
        </div>
      )}
    </div>
  );
}
