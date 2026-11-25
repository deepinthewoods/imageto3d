// Client-side thumbnails for 3D-stage cards. A card whose `thumbnail` is null but whose `model` is set gets one
// rendered offscreen here (one at a time), shown at once and saved on the server (POST /api/v2/thumbnail) so the
// next state response already carries it.
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { api } from './api.js';
import { addLights, disposeObject } from './viewer.js';

const SIZE = 512;
let renderer = null;
let chain = Promise.resolve();
const jobs = new Map();      // model url -> Promise<image url | null>
const failed = new Set();    // never retried in this page load

function getRenderer() {
  if (!renderer) {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
    renderer.setPixelRatio(1);
    renderer.setSize(SIZE, SIZE);
    renderer.setClearColor(0x000000, 0);
  }
  return renderer;
}

async function render(url) {
  const gltf = await new GLTFLoader().loadAsync(url);
  try {
    const scene = new THREE.Scene();
    addLights(scene);
    scene.add(gltf.scene);
    const box = new THREE.Box3().setFromObject(gltf.scene);
    const center = box.getCenter(new THREE.Vector3());
    const extent = box.getSize(new THREE.Vector3());
    const distance = Math.max(extent.x, extent.y) / 2 / Math.tan(THREE.MathUtils.degToRad(15)) * 1.08 + extent.z / 2;
    const camera = new THREE.PerspectiveCamera(30, 1, distance / 100, distance * 10);
    camera.position.copy(center).add(new THREE.Vector3(0.25, 0.1, 1).normalize().multiplyScalar(distance));
    camera.lookAt(center);
    const gl = getRenderer();
    gl.render(scene, camera);
    return await new Promise(done => gl.domElement.toBlob(done, 'image/png'));
  } finally {
    disposeObject(gltf.scene);
  }
}

async function makeThumbnail(url) {
  const blob = await render(url);
  if (!blob) throw new Error('canvas gave no PNG');
  const result = await api.thumbnail(url, blob);
  return result.image;
}

/** Resolves to the saved thumbnail URL, or null when rendering or saving failed. Calls for one model share a job. */
export function requestThumbnail(modelUrl) {
  if (failed.has(modelUrl)) return Promise.resolve(null);
  if (!jobs.has(modelUrl)) {
    const job = chain
      .then(() => makeThumbnail(modelUrl))
      .catch(error => {
        console.warn('Thumbnail failed', modelUrl, error);
        failed.add(modelUrl);
        jobs.delete(modelUrl);
        return null;
      })
      .then(async image => {
        await new Promise(resolve => setTimeout(resolve, 30)); // let the UI breathe between models
        return image;
      });
    jobs.set(modelUrl, job);
    chain = job;
  }
  return jobs.get(modelUrl);
}

/** Thumbnails made in this page load (model url -> image url); cards drawn again before the next poll use them. */
export const rendered = new Map();

/** Show the thumbnail for `card` in `img` once the card is near the viewport. */
const observer = typeof IntersectionObserver === 'function'
  ? new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      observer.unobserve(entry.target);
      entry.target._start?.();
    }
  }, { rootMargin: '300px' })
  : null;

export function attachThumbnail(img, modelUrl, { keepSrc = false } = {}) {
  if (rendered.has(modelUrl)) { img.src = rendered.get(modelUrl); return; }
  if (!keepSrc) img.classList.add('pending');      // keepSrc: the img already shows a fallback picture; only swap it when the render is ready
  const start = () => requestThumbnail(modelUrl).then(image => {
    if (!image) { if (!keepSrc) img.classList.add('missing'); return; }
    rendered.set(modelUrl, image);
    img.src = image;
    img.classList.remove('pending');
  });
  if (observer) { img._start = start; observer.observe(img); } else start();
}
