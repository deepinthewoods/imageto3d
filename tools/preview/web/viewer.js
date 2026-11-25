// three.js viewer shared by the 3D, Aligned/Completed and Review modals.
//   createViewer(container) -> { load, clear, setWireframe, setMorph, getView, setView, resetView, dispose }
// Models are drawn flat shaded and double sided; a pixel seen from behind is painted red (see backRed), which is
// exactly where a face points the wrong way or the garment is inside out. Same trick as the layers prototype
// (experiments/shell_slots_20261122/out/layers/mix.html).
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

/**
 * Back faces red without a second mesh: one double-sided material whose fragment shader paints back-facing pixels red.
 * Nothing can drift with a morph or z-fight. Idempotent.
 */
export function backRed(material) {
  if (material.userData.backRed) return material;
  material.userData.backRed = true;
  material.side = THREE.DoubleSide;
  material.onBeforeCompile = shader => {
    shader.fragmentShader = shader.fragmentShader.replace(
      '#include <dithering_fragment>',
      '#include <dithering_fragment>\n\tif (!gl_FrontFacing) gl_FragColor = vec4(1.0, 0.0, 0.0, 1.0);');
  };
  material.customProgramCacheKey = () => 'backred';
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
  let loads = 0;        // stale-load guard: only the newest load() may touch the scene
  let wire = false;
  let hasModel = false;
  const morphs = new Map();
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

  (function frame() {
    if (!running) return;
    controls.update();
    renderer.render(scene, camera);
    requestAnimationFrame(frame);
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
        backRed(material);
      }
    });
  }

  function applyMorphs() {
    group.traverse(node => {
      if (!node.isMesh || !node.morphTargetDictionary) return;
      for (const [name, value] of morphs) {
        if (name in node.morphTargetDictionary) node.morphTargetInfluences[node.morphTargetDictionary[name]] = value;
      }
    });
  }

  function clear() {
    ++loads;
    for (const child of [...group.children]) {
      group.remove(child);
      disposeObject(child);
    }
    hasModel = false;
  }

  /**
   * Replace the model by the glTF files at `urls`. The camera is kept relative to the model's size, so stepping
   * through LODs, races or sets does not jump; `view` forces one (from getView()), `reframe` forces the home view.
   * Resolves {applied: false} when a newer load() or clear() overtook this one; rejects when a file cannot be read.
   */
  async function load(urls, { view = null, reframe = false } = {}) {
    const mine = ++loads;
    const loaded = await Promise.all([].concat(urls).map(url => loader.loadAsync(url)));
    if (mine !== loads || !running) {
      loaded.forEach(gltf => disposeObject(gltf.scene));
      return { applied: false };
    }
    const keep = reframe ? null : (view || (hasModel ? getView() : null));
    for (const child of [...group.children]) {
      group.remove(child);
      disposeObject(child);
    }
    for (const gltf of loaded) {
      prepare(gltf.scene);
      group.add(gltf.scene);
    }
    hasModel = true;
    applyMorphs();
    const box = new THREE.Box3().setFromObject(group);
    center = box.getCenter(new THREE.Vector3());
    radius = Math.max(box.getSize(new THREE.Vector3()).length() * 0.8, 0.1);
    camera.near = Math.max(radius / 1000, 0.001);
    camera.far = radius * 100;
    camera.updateProjectionMatrix();
    if (keep) setView(keep); else applyHomeView();
    return { applied: true };
  }

  renderer.domElement.addEventListener('dblclick', applyHomeView);

  return {
    load,
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
    dispose() {
      running = false;
      ++loads;
      observer.disconnect();
      controls.dispose();
      for (const child of [...group.children]) disposeObject(child);
      group.clear();
      renderer.dispose();
      renderer.forceContextLoss();
      renderer.domElement.remove();
    },
  };
}
