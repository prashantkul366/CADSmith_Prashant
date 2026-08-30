"use strict";
/* ═══════════════════════════════════════════════════════════════════════
   3D viewer — displays the STL the OpenCASCADE kernel actually exported.

   The camera, orbit bindings and axis gizmo are carried over unchanged from
   the original Text2CAD shell, so the interaction feel is identical.  What
   changed is where geometry comes from: nothing is constructed in the
   browser any more.  The mesh on screen is the exported solid, so it cannot
   disagree with the CadQuery source shown beside it.

   Z is up, matching CadQuery's convention.
   ═══════════════════════════════════════════════════════════════════════ */

const Viewer = (() => {
  const host = document.querySelector("#gl");
  const scene = new THREE.Scene();
  const renderer = new THREE.WebGLRenderer({
    antialias: true, alpha: true, preserveDrawingBuffer: true,
  });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  host.appendChild(renderer.domElement);

  const cam = new THREE.PerspectiveCamera(38, 1, 0.5, 20000);
  const target = new THREE.Vector3();
  let dist = 300, theta = -Math.PI * 0.28, phi = Math.PI * 0.34;
  let spin = true, wire = false;

  scene.add(new THREE.HemisphereLight(0xBFD4EE, 0x141821, 0.85));
  const key = new THREE.DirectionalLight(0xffffff, 0.95);
  key.position.set(1, 0.7, 1.4); scene.add(key);
  const fill = new THREE.DirectionalLight(0x9FC0EA, 0.4);
  fill.position.set(-1.2, -0.6, 0.4); scene.add(fill);
  const rim = new THREE.DirectionalLight(0xffffff, 0.28);
  rim.position.set(0, -1, -1); scene.add(rim);

  const grid = new THREE.GridHelper(600, 30, 0x2A3442, 0x1A2029);
  grid.rotation.x = Math.PI / 2; grid.position.z = -0.4;
  grid.material.transparent = true; grid.material.opacity = 0.55;
  scene.add(grid);

  let model = null, radius = 100, extents = null;

  /* ── STL parsing ──────────────────────────────────────────────────────
     CadQuery writes binary STL (80-byte header, uint32 triangle count, then
     50 bytes per facet).  ASCII is handled too, since a hand-supplied file
     may use it.  Parsing here rather than pulling in a loader keeps the app
     free of a second CDN dependency.                                      */

  function isAscii(buffer) {
    // A binary STL's size is exactly 84 + 50n. Trust that over sniffing for
    // the word "solid", which some binary writers also put in the header.
    if (buffer.byteLength < 84) return true;
    const count = new DataView(buffer).getUint32(80, true);
    if (84 + count * 50 === buffer.byteLength) return false;
    const head = new TextDecoder().decode(new Uint8Array(buffer, 0, 5));
    return head.trim().toLowerCase() === "solid";
  }

  function parseBinary(buffer) {
    const view = new DataView(buffer);
    const count = view.getUint32(80, true);
    const positions = new Float32Array(count * 9);
    const normals = new Float32Array(count * 9);
    let offset = 84;
    for (let i = 0; i < count; i++) {
      const nx = view.getFloat32(offset, true);
      const ny = view.getFloat32(offset + 4, true);
      const nz = view.getFloat32(offset + 8, true);
      offset += 12;
      for (let v = 0; v < 3; v++) {
        const p = i * 9 + v * 3;
        positions[p]     = view.getFloat32(offset, true);
        positions[p + 1] = view.getFloat32(offset + 4, true);
        positions[p + 2] = view.getFloat32(offset + 8, true);
        normals[p] = nx; normals[p + 1] = ny; normals[p + 2] = nz;
        offset += 12;
      }
      offset += 2; // attribute byte count
    }
    return { positions, normals };
  }

  function parseAscii(buffer) {
    const text = new TextDecoder().decode(new Uint8Array(buffer));
    const positions = [], normals = [];
    const facets = text.split(/facet\s+normal/i).slice(1);
    for (const facet of facets) {
      const n = facet.trim().split(/\s+/).slice(0, 3).map(Number);
      const verts = [...facet.matchAll(
        /vertex\s+(-?[\d.eE+]+)\s+(-?[\d.eE+]+)\s+(-?[\d.eE+]+)/gi)];
      for (const v of verts.slice(0, 3)) {
        positions.push(+v[1], +v[2], +v[3]);
        normals.push(n[0] || 0, n[1] || 0, n[2] || 1);
      }
    }
    return {
      positions: new Float32Array(positions),
      normals: new Float32Array(normals),
    };
  }

  function buildGeometry(buffer) {
    const { positions, normals } =
      isAscii(buffer) ? parseAscii(buffer) : parseBinary(buffer);
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute("normal", new THREE.BufferAttribute(normals, 3));
    geometry.computeBoundingBox();
    return geometry;
  }

  /* ── model management ─────────────────────────────────────────────── */

  function disposeAll(object) {
    object.traverse(node => {
      if (node.geometry) node.geometry.dispose();
      if (node.material) {
        (Array.isArray(node.material) ? node.material : [node.material])
          .forEach(m => m.dispose());
      }
    });
  }

  function eachMaterial(fn) {
    if (!model) return;
    model.traverse(node => {
      if (node.isMesh) fn(node.material, false);
      if (node.isLineSegments) fn(node.material, true);
    });
  }

  function applyModes() {
    eachMaterial((m, isLine) => {
      m.needsUpdate = true;
      if (isLine) {
        m.opacity = wire ? 0.95 : 0.32;
      } else {
        m.transparent = wire;
        m.opacity = wire ? 0.06 : 1;
        m.depthWrite = !wire;
      }
    });
  }

  async function load(url) {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`Could not load model (${response.status})`);
    const geometry = buildGeometry(await response.arrayBuffer());

    const group = new THREE.Group();
    const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({
      color: 0x8FB4E8, metalness: 0.22, roughness: 0.55,
      flatShading: false, side: THREE.DoubleSide,
    }));
    group.add(mesh);

    // Feature edges at 22 degrees, the same threshold the original shell used.
    const edges = new THREE.LineSegments(
      new THREE.EdgesGeometry(geometry, 22),
      new THREE.LineBasicMaterial({
        color: 0xD6E2F2, transparent: true, opacity: 0.32,
      }));
    group.add(edges);

    setModel(group, geometry.boundingBox);
    return geometry.boundingBox;
  }

  function setModel(group, box) {
    if (model) { scene.remove(model); disposeAll(model); }
    model = group; scene.add(group);

    const centre = box.getCenter(new THREE.Vector3());
    group.position.sub(centre);          // orbit about the part, not the origin
    const size = box.getSize(new THREE.Vector3());
    extents = size;
    radius = Math.max(size.x, size.y, size.z, 1) * 0.62;
    grid.scale.setScalar(Math.max(0.5, radius / 100));
    applyModes();

    const step = Math.max(5, Math.round(radius / 2 / 5) * 5);
    const scaleText = document.querySelector("#scaleTxt");
    if (scaleText) scaleText.textContent = `${step} mm`;
  }

  function clear() {
    if (model) { scene.remove(model); disposeAll(model); model = null; }
    extents = null;
  }

  /* ── camera ───────────────────────────────────────────────────────── */

  const ease = k => 1 - Math.pow(1 - k, 3);
  let tween = null;

  function fit(animate) {
    const d = radius / Math.tan(cam.fov * Math.PI / 360) * 1.45;
    if (!animate) { dist = d; return; }
    const from = dist, t0 = performance.now();
    tween = () => {
      const k = Math.min(1, (performance.now() - t0) / 420);
      dist = from + (d - from) * ease(k);
      if (k >= 1) tween = null;
    };
  }

  function view(name, animate) {
    const angles = {
      iso: [-Math.PI * 0.28, Math.PI * 0.34],
      front: [-Math.PI / 2, Math.PI / 2],
      top: [-Math.PI / 2, 0.0001],
      right: [0, Math.PI / 2],
    }[name];
    if (!angles) return;
    spin = false;
    if (!animate) { theta = angles[0]; phi = angles[1]; return; }
    const t0 = performance.now(), from = [theta, phi];
    let to = angles[0];
    while (to - from[0] > Math.PI) to -= Math.PI * 2;
    while (to - from[0] < -Math.PI) to += Math.PI * 2;
    tween = () => {
      const k = Math.min(1, (performance.now() - t0) / 480), e = ease(k);
      theta = from[0] + (to - from[0]) * e;
      phi = from[1] + (angles[1] - from[1]) * e;
      if (k >= 1) tween = null;
    };
  }

  /* LMB rotate · MMB/Ctrl pan · RMB/Shift zoom · wheel zoom */
  let drag = null;
  const canvas = renderer.domElement;
  canvas.addEventListener("contextmenu", e => e.preventDefault());
  canvas.addEventListener("pointerdown", e => {
    canvas.setPointerCapture(e.pointerId);
    const mode = (e.button === 1 || e.ctrlKey) ? "pan"
               : (e.button === 2 || e.shiftKey) ? "zoom" : "rot";
    drag = { mode, x: e.clientX, y: e.clientY };
    spin = false;
    document.querySelector("#spinBtn").classList.remove("on");
  });
  canvas.addEventListener("pointermove", e => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    drag.x = e.clientX; drag.y = e.clientY;
    if (drag.mode === "rot") {
      theta -= dx * 0.0075;
      phi = Math.max(0.02, Math.min(Math.PI - 0.02, phi - dy * 0.0075));
    } else if (drag.mode === "zoom") {
      dist = Math.max(radius * 0.35, Math.min(radius * 22, dist * (1 + dy * 0.006)));
    } else {
      const s = dist * 0.0016;
      const right = new THREE.Vector3()
        .subVectors(cam.position, target).cross(cam.up).normalize();
      const up = new THREE.Vector3().crossVectors(
        right, new THREE.Vector3().subVectors(cam.position, target)).normalize();
      target.addScaledVector(right, -dx * s).addScaledVector(up, -dy * s);
    }
  });
  addEventListener("pointerup", () => { drag = null; });
  canvas.addEventListener("wheel", e => {
    e.preventDefault();
    dist = Math.max(radius * 0.35,
      Math.min(radius * 22, dist * (1 + Math.sign(e.deltaY) * 0.11)));
  }, { passive: false });

  function resize() {
    const w = host.clientWidth, h = host.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    cam.aspect = w / h;
    cam.updateProjectionMatrix();
  }
  new ResizeObserver(resize).observe(host);
  resize();

  const axisGroup = document.querySelector("#axG");
  function drawAxes() {
    const R = 20, cx = 30, cy = 30;
    const dirs = [
      ["X", new THREE.Vector3(1, 0, 0), "#F2606A"],
      ["Y", new THREE.Vector3(0, 1, 0), "#3DD68C"],
      ["Z", new THREE.Vector3(0, 0, 1), "#4D8DF6"],
    ];
    let svg = "";
    for (const [name, vector, colour] of dirs) {
      const p = vector.clone().project(cam);
      const x = cx + p.x * R, y = cy - p.y * R;
      svg += `<line x1="${cx}" y1="${cy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" stroke="${colour}"/>`;
      svg += `<text x="${(cx + (x - cx) * 1.32).toFixed(1)}" y="${(cy + (y - cy) * 1.32 + 3).toFixed(1)}" fill="${colour}" font-size="8" font-family="monospace" text-anchor="middle" stroke="none">${name}</text>`;
    }
    axisGroup.innerHTML = svg;
  }

  (function loop() {
    requestAnimationFrame(loop);
    if (tween) tween();
    if (spin && model) theta += 0.0034;
    cam.position.set(
      target.x + dist * Math.sin(phi) * Math.cos(theta),
      target.y + dist * Math.sin(phi) * Math.sin(theta),
      target.z + dist * Math.cos(phi));
    cam.up.set(0, 0, 1);
    cam.lookAt(target);
    renderer.render(scene, cam);
    drawAxes();
  })();

  return {
    load, clear, fit, view,
    get spin() { return spin; },
    set spin(v) { spin = v; },
    get extents() { return extents; },
    toggleWire() { wire = !wire; applyModes(); return wire; },
    snapshot(white) {
      const background = scene.background;
      if (white) {
        scene.background = new THREE.Color(0xffffff);
        eachMaterial((m, isLine) => {
          if (isLine) { m.color.set(0x000000); m.opacity = 1; }
          else { m.transparent = true; m.opacity = 0; }
        });
      }
      renderer.render(scene, cam);
      const url = renderer.domElement.toDataURL("image/png");
      if (white) {
        scene.background = background;
        eachMaterial((m, isLine) => { if (isLine) m.color.set(0xD6E2F2); });
        applyModes();
      }
      return url;
    },
  };
})();
