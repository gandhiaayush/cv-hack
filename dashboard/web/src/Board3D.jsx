import React, { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";

// KiCad's GLB is in metres, Y up; board X/Y (mm) map to GLB X/Z.
const MM = 0.001;

export default function Board3D({ runId, designVersion, failing = [] }) {
  const mount = useRef(null);
  const ctx = useRef(null);
  const [status, setStatus] = useState("Building board in KiCad…");
  const [info, setInfo] = useState(null);
  const [picked, setPicked] = useState(null);
  const [hover, setHover] = useState(null);

  // Scene setup once.
  useEffect(() => {
    const el = mount.current;
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(window.devicePixelRatio);
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    el.appendChild(renderer.domElement);
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0d1117);
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
    const loop = () => { controls.update(); renderer.render(scene, camera); raf = requestAnimationFrame(loop); };
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
        let o = hit.object;
        while (o && !o.userData.ref) o = o.parent;
        if (o) return o.userData.ref;
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

    ctx.current = { renderer, scene, camera, controls, root: null, parts: {} };
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      controls.dispose();
      renderer.dispose();
      el.removeChild(renderer.domElement);
    };
  }, []);

  // (Re)load the board whenever the design changes.
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    setStatus("Building board in KiCad…");
    setPicked(null);
    fetch(`/api/runs/${runId}/board.json`)
      .then((r) => (r.ok ? r.json() : r.text().then((t) => Promise.reject(new Error(t)))))
      .then((meta) => {
        if (cancelled) return;
        setInfo(meta);
        new GLTFLoader().load(`/api/runs/${runId}/board.glb?v=${meta.glb_version}`, (gltf) => {
          if (cancelled) return;
          const c = ctx.current;
          if (c.root) c.scene.remove(c.root);
          const root = gltf.scene;
          const parts = {};
          root.traverse((o) => {
            if (meta.placements[o.name]) { o.userData.ref = o.name; parts[o.name] = o; }
          });
          // Parts without a KiCad 3D model get a plain block so nothing is invisible.
          const top = (meta.thickness_mm || 1.6) * MM;
          for (const [ref, p] of Object.entries(meta.placements)) {
            if (parts[ref]) continue;
            const hgt = Math.min(Math.max(Math.min(p.w, p.h) * 0.25, 0.8), 2.5) * MM;
            const box = new THREE.Mesh(
              new THREE.BoxGeometry(p.w * 0.9 * MM, hgt, p.h * 0.9 * MM),
              new THREE.MeshStandardMaterial({ color: 0x2b2f36, roughness: 0.6, metalness: 0.1 }));
            box.position.set(p.x * MM, top + hgt / 2, p.y * MM);
            box.name = ref;
            box.userData.ref = ref;
            box.userData.placeholder = true;
            root.add(box);
            parts[ref] = box;
          }
          c.scene.add(root);
          c.root = root;
          c.parts = parts;
          // Frame the board.
          const w = meta.width_mm * MM, h = meta.height_mm * MM;
          const center = new THREE.Vector3(w / 2, 0, h / 2);
          c.controls.target.copy(center);
          const fov = (c.camera.fov * Math.PI) / 180;
          const span = Math.max(w / Math.max(c.camera.aspect, 0.5), h) * 0.5;
          const dist = (span / Math.tan(fov / 2)) * 1.35;
          c.camera.position.set(center.x, dist * 0.82, center.z + dist * 0.57);
          c.controls.update();
          setStatus(null);
        }, undefined, (err) => setStatus(`Could not load GLB: ${err.message || err}`));
      })
      .catch((e) => setStatus(e.message.includes("KiCad") ? e.message : `Board build failed: ${e.message}`));
    return () => { cancelled = true; };
  }, [runId, designVersion]);

  // Tint failing, hovered and picked parts.
  useEffect(() => {
    const c = ctx.current;
    if (!c?.root) return;
    const bad = new Set(failing);
    for (const [ref, o] of Object.entries(c.parts)) {
      const color = ref === picked ? 0x6aa8ff : bad.has(ref) ? 0xf85149 : ref === hover ? 0x3b82f6 : null;
      o.traverse((m) => {
        if (!m.isMesh) return;
        if (!m.userData.baseMat) { m.userData.baseMat = m.material; }
        if (color == null) { m.material = m.userData.baseMat; return; }
        const mat = m.userData.baseMat.clone();
        if (mat.emissive) { mat.emissive.setHex(color); mat.emissiveIntensity = ref === hover && ref !== picked ? 0.35 : 0.6; }
        m.material = mat;
      });
    }
    c.renderer.domElement.style.cursor = hover ? "pointer" : "grab";
  }, [failing, hover, picked, status]);

  const p = picked && info?.placements?.[picked];
  return (
    <div className="board3d">
      <div ref={mount} className="board3d-canvas" />
      {status && <div className="board3d-status">{status}</div>}
      {info && !status && (
        <div className="board3d-meta">
          {info.width_mm} × {info.height_mm} mm · {info.footprints} parts · ground poured, not routed
          {info.models_missing?.length > 0 && <span className="muted"> · {info.models_missing.length} without KiCad 3D model (grey blocks)</span>}
          <div className="muted small">drag to orbit · scroll to zoom · right-drag to pan · click a part</div>
        </div>
      )}
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
