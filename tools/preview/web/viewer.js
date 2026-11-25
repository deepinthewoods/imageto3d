// three.js viewer shared by the 3D, Aligned/Completed, Mix and Review views.
//   createViewer(container) -> { load, compose, clear, setWireframe, setMorph, setLayerMorph, setMorphHold, setBackRed,
//                                setColourByLayer, getView, setView, resetView, dispose }
//   load(urls)      one scene made of the files at `urls` (3D modal, monsters, review);
//   compose(layers) a character made of layers [{key, url}]: one group and one camera; a layer is only read again when its url
//                   changed; every layer has its own `fill` morph weight (the Amount); replaced layers are disposed; a newer
//                   call overtakes an older one (stale-load guard).
// Models are drawn flat shaded and double sided; a pixel seen from behind is painted red (see backRed), which is
// exactly where a face points the wrong way or the garment is inside out. Same trick as the layers prototype
// (experiments/shell_slots_20261122/out/layers/mix.html).
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

/**
 * Back faces red without a second mesh: one double-sided material whose fragment shader paints back-facing pixels red.
 * Nothing can drift with a morph or z-fight. `material.userData.backRedUniform.value` = 0 switches the red off. Idempotent.
 */
export function backRed(material) {
  if (material.userData.backRed) return material;
  material.userData.backRed = true;
  material.userData.backRedUniform = { value: 1 };
  material.side = THREE.DoubleSide;
  material.onBeforeCompile = shader => {
    shader.uniforms.uBackRed = material.userData.backRedUniform;
    shader.fragmentShader = 'uniform float uBackRed;\n' + shader.fragmentShader.replace(
      '#include <dithering_fragment>',
      '#include <dithering_fragment>\n\tif (uBackRed > 0.5 && !gl_FrontFacing) gl_FragColor = vec4(1.0, 0.0, 0.0, 1.0);');
  };
  material.customProgramCacheKey = () => 'backred2';
  material.needsUpdate = true;
  return material;
}

export function addLights(scene) {
  scene.add(new THREE.HemisphereLight(0xffffff, 0x66748b, 2));
  const key = new THREE.DirectionalLight(0xffffff, 2.1);
  key.position.set(4, 8, 6);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xb6cbff, 0.8);
  fill.position.set(-5, 2, -4);
  scene.add(fill);
}

const materialsOf = mesh => (Array.isArray(mesh.material) ? mesh.material : [mesh.material]);

/** Free GPU memory of a loaded glTF scene (geometries, materials, textures). */
export function disposeObject(root) {
  root.traverse(node => {
    if (!node.isMesh) return;
    node.geometry?.dispose();
    for (const material of materialsOf(node)) {
      for (const value of Object.values(material)) if (value?.isTexture) value.dispose();
      material.dispose();
    }
  });
}

/** One colour per layer slot for "colour by layer" (stable, so a slot keeps its colour when another set's piece replaces it). */
const TINTS = ['#e6194b', '#3cb44b', '#ffe119', '#4363d8', '#f58231', '#911eb4', '#42d4f4', '#f032e6', '#bfef45', '#fabed4'];
const SKIN_TINT = '#cfc6bd';
export const tintOf = key => {
  let n = 0;
  for (const c of String(key)) n = (n * 31 + c.charCodeAt(0)) >>> 0;
  return TINTS[n % TINTS.length];
};

export function createViewer(container, { background = '#323944' } = {}) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(background);
  addLights(scene);
  const camera = new THREE.PerspectiveCamera(42, 1, 0.01, 1000);
  // No MSAA (it draws back faces as red hairlines on silhouettes): render at >= 2x and let the browser scale down.
  const renderer = new THREE.WebGLRenderer({ antialias: false });
  renderer.setPixelRatio(Math.max(devicePixelRatio || 1, 2));
  container.prepend(renderer.domElement);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  const loader = new GLTFLoader();
  const group = new THREE.Group();
  scene.add(group);

  let center = new THREE.Vector3();
  let radius = 1;
  let loads = 0;        // stale-load guard: only the newest load() / compose() may touch the scene
  let wire = false;
  let hasModel = false;
  let backsRed = true;
  let tinted = false;
  let hold = null;                  // a number overrides every layer's `fill` weight (hold-to-compare)
  const morphs = new Map();         // load(): morph name -> weight for every mesh
  const layers = new Map();         // compose(): key -> {url, root, tint, faces}
  const layerMorph = new Map();     // compose(): key -> weight 0..1
  let running = true;
  let observer = null;

  function resize() {
    const w = container.clientWidth;
    const h = container.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  observer = new ResizeObserver(resize);
  observer.observe(container);
  resize();

  (function frameLoop() {
    if (!running) return;
    controls.update();
    renderer.render(scene, camera);
    requestAnimationFrame(frameLoop);
  })();

  function applyHomeView() {
    camera.position.copy(center).add(new THREE.Vector3(radius * 0.65, radius * 0.3, radius * 1.5));
    controls.target.copy(center);
    controls.update();
  }

  const getView = () => ({
    offset: camera.position.clone().sub(center).divideScalar(radius),
    aim: controls.target.clone().sub(center).divideScalar(radius),
  });

  function setView(view) {
    camera.position.copy(center).addScaledVector(view.offset, radius);
    controls.target.copy(center).addScaledVector(view.aim, radius);
    controls.update();
  }

  function prepare(root) {
    const seen = new Set();
    root.traverse(node => {
      if (!node.isMesh) return;
      for (const material of materialsOf(node)) {
        if (seen.has(material)) continue;
        seen.add(material);
        material.flatShading = true;
        material.wireframe = wire;
        backRed(material).userData.backRedUniform.value = backsRed ? 1 : 0;
        material.userData.baseColor ??= material.color?.clone();
      }
    });
  }

  const faceCount = root => {
    let n = 0;
    root.traverse(node => {
      if (node.isMesh) n += (node.geometry.index?.count ?? node.geometry.attributes.position.count) / 3;
    });
    return Math.round(n);
  };

  /** Colour by layer: every layer one flat colour (its skin pale grey), or back to the colours of the file. */
  function applyTint() {
    for (const layer of layers.values()) {
      layer.root.traverse(node => {
        if (!node.isMesh) return;
        for (const material of materialsOf(node)) {
          if (!material.userData.baseColor) continue;
          if (tinted) material.color.set(String(material.name).startsWith('skin') ? SKIN_TINT : layer.tint);
          else material.color.copy(material.userData.baseColor);
        }
      });
    }
  }

  function applyMorphs() {
    group.traverse(node => {
      if (!node.isMesh || !node.morphTargetDictionary) return;
      let owner = node;
      while (owner && owner.userData.layerKey === undefined) owner = owner.parent;
      if (owner && 'fill' in node.morphTargetDictionary) {
        node.morphTargetInfluences[node.morphTargetDictionary.fill] = hold ?? layerMorph.get(owner.userData.layerKey) ?? 1;
        return;
      }
      for (const [name, value] of morphs) {
        if (name in node.morphTargetDictionary) node.morphTargetInfluences[node.morphTargetDictionary[name]] = value;
      }
    });
  }

  function removeAll() {
    for (const child of [...group.children]) {
      group.remove(child);
      disposeObject(child);
    }
    layers.clear();
  }

  function clear() {
    ++loads;
    removeAll();
    container.dataset.layers = '';
    hasModel = false;
  }

  function reframe() {
    const box = new THREE.Box3().setFromObject(group);
    center = box.getCenter(new THREE.Vector3());
    radius = Math.max(box.getSize(new THREE.Vector3()).length() * 0.8, 0.1);
    camera.near = Math.max(radius / 1000, 0.001);
    camera.far = radius * 100;
    camera.updateProjectionMatrix();
  }

  /**
   * Replace the model by the glTF files at `urls`. The camera is kept relative to the model's size, so stepping
   * through LODs, races or sets does not jump; `view` forces one (from getView()), `reframe` forces the home view.
   * Resolves {applied: false} when a newer load() or clear() overtook this one; rejects when a file cannot be read.
   */
  async function load(urls, { view = null, reframe: home = false } = {}) {
    const mine = ++loads;
    const loaded = await Promise.all([].concat(urls).map(url => loader.loadAsync(url)));
    if (mine !== loads || !running) {
      loaded.forEach(gltf => disposeObject(gltf.scene));
      return { applied: false };
    }
    const keep = home ? null : (view || (hasModel ? getView() : null));
    removeAll();
    for (const gltf of loaded) {
      prepare(gltf.scene);
      group.add(gltf.scene);
    }
    hasModel = true;
    applyMorphs();
    reframe();
    if (keep) setView(keep); else applyHomeView();
    return { applied: true };
  }

  /**
   * Show the character made of `layers` [{key, url}] (keys are unique: a slot name). Layers whose key and url are unchanged stay
   * (no re-read, no flicker); the others are replaced and disposed. A layer that cannot be read is left out and reported in `failed`
   * ({key, url, error}); the mesh it was to replace is removed too, so the scene never keeps the previous geometry under the new selection.
   * Camera: the same absolute view when only some layers changed; `view` (from getView()) or `relative: true` keep the view relative to
   * the model's size (a race of another size, a new viewer); `reframe: true` goes home.
   * Resolves {applied, failed: [{key, error}], info: {key: {faces, url}}}; applied false when a newer call overtook this one.
   */
  async function compose(wanted, { view = null, relative = false, reframe: home = false } = {}) {
    const mine = ++loads;
    const todo = wanted.filter(layer => layers.get(layer.key)?.url !== layer.url);
    const results = await Promise.all(todo.map(async layer => {
      try { return { layer, gltf: await loader.loadAsync(layer.url) }; } catch (error) { return { layer, error }; }
    }));
    if (mine !== loads || !running) {
      results.forEach(r => r.gltf && disposeObject(r.gltf.scene));
      return { applied: false, failed: [], info: {} };
    }
    const keys = new Set(wanted.map(layer => layer.key));
    const absolute = !home && !view && !relative && hasModel
      ? { position: camera.position.clone(), target: controls.target.clone() } : null;
    const keep = home ? null : (view || (!absolute && hasModel ? getView() : null));
    for (const [key, entry] of [...layers]) {
      const gone = !keys.has(key) || results.some(r => r.layer.key === key);   // replaced, or its replacement failed to load
      if (!gone) continue;
      group.remove(entry.root);
      disposeObject(entry.root);
      layers.delete(key);
    }
    const failed = [];
    for (const { layer, gltf, error } of results) {
      if (!gltf) { failed.push({ key: layer.key, url: layer.url, error: error?.message || String(error) }); continue; }
      const root = gltf.scene;
      root.userData.layerKey = layer.key;
      prepare(root);
      group.add(root);
      layers.set(layer.key, { url: layer.url, root, tint: tintOf(layer.key), faces: faceCount(root) });
    }
    hasModel = group.children.length > 0;
    for (const layer of wanted) if (typeof layer.morph === 'number') layerMorph.set(layer.key, layer.morph);
    applyMorphs();
    applyTint();
    reframe();
    if (absolute) {
      camera.position.copy(absolute.position);
      controls.target.copy(absolute.target);
      controls.update();
    } else if (keep) setView(keep); else applyHomeView();
    const info = Object.fromEntries([...layers].map(([key, entry]) => [key, { faces: entry.faces, url: entry.url }]));
    container.dataset.layers = [...layers.keys()].join(',');      // which slots really hold a mesh (read by the browser tests)
    return { applied: true, failed, info };
  }

  renderer.domElement.addEventListener('dblclick', applyHomeView);

  return {
    load,
    compose,
    clear,
    getView,
    setView,
    resetView: applyHomeView,
    get hasModel() { return hasModel; },
    setWireframe(on) {
      wire = on;
      group.traverse(node => { if (node.isMesh) for (const m of materialsOf(node)) m.wireframe = on; });
    },
    /** Set a morph target weight on every mesh that has it (by name, e.g. "fill"); remembered across loads. */
    setMorph(name, value) {
      morphs.set(name, value);
      applyMorphs();
    },
    /** The `fill` weight (0..1) of one composed layer (the Amount); remembered across compose() calls. */
    setLayerMorph(key, value) {
      layerMorph.set(key, value);
      applyMorphs();
    },
    /** A number forces every composed layer's `fill` weight (hold to compare with Amount 0); null releases it. */
    setMorphHold(value) {
      hold = value;
      applyMorphs();
    },
    /** Paint the inside of faces red (default on). */
    setBackRed(on) {
      backsRed = !!on;
      group.traverse(node => {
        if (!node.isMesh) return;
        for (const m of materialsOf(node)) if (m.userData.backRedUniform) m.userData.backRedUniform.value = backsRed ? 1 : 0;
      });
    },
    /** Flat colour per composed layer instead of the colours of the file. */
    setColourByLayer(on) {
      tinted = !!on;
      applyTint();
    },
    dispose() {
      running = false;
      ++loads;
      observer.disconnect();
      controls.dispose();
      removeAll();
      renderer.dispose();
      renderer.forceContextLoss();
      renderer.domElement.remove();
    },
  };
}
