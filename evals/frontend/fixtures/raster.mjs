// Original mesh and GLSL. Only projection, view and object matrices come from the reviewed camera subset.
import { PerspectiveCamera } from './three/src/cameras/PerspectiveCamera.js';
import { Object3D } from './three/src/core/Object3D.js';

const canvas = document.querySelector('canvas');
const button = document.querySelector('button');
const output = document.querySelector('output');
const gl = canvas.getContext('webgl2', { antialias: false, preserveDrawingBuffer: true, alpha: false });

function compile(type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader));
  return shader;
}

function mount() {
  const vertex = compile(gl.VERTEX_SHADER, `#version 300 es
    layout(location=0) in vec3 position;
    layout(location=1) in vec3 normal;
    layout(location=2) in vec3 pigment;
    uniform mat4 projection, view, model;
    out vec3 shade;
    void main() {
      vec3 light = normalize(vec3(-0.4,0.8,0.65));
      float diffuse = max(dot(normalize(mat3(model)*normal),light),0.0);
      shade = pigment * (0.50 + 0.50 * diffuse);
      gl_Position = projection * view * model * vec4(position,1.0);
    }`);
  const fragment = compile(gl.FRAGMENT_SHADER, `#version 300 es
    precision mediump float;
    in vec3 shade;
    out vec4 color;
    void main() { color=vec4(shade,1.0); }`);
  const program = gl.createProgram();
  gl.attachShader(program, vertex);
  gl.attachShader(program, fragment);
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
  gl.useProgram(program);

  const corners = [[-1,-1,1],[1,-1,1],[1,1,1],[-1,1,1],[-1,-1,-1],[1,-1,-1],[1,1,-1],[-1,1,-1]];
  const faces = [
    [[0,1,2,3],[0,0,1],[.76,.42,.25]], [[5,4,7,6],[0,0,-1],[.22,.34,.30]],
    [[1,5,6,2],[1,0,0],[.64,.56,.41]], [[4,0,3,7],[-1,0,0],[.41,.48,.41]],
    [[3,2,6,7],[0,1,0],[.87,.79,.64]], [[4,5,1,0],[0,-1,0],[.39,.36,.29]]
  ];
  const vertices = [];
  for (const [face, normal, pigment] of faces) {
    for (const index of [0,1,2,0,2,3]) vertices.push(...corners[face[index]], ...normal, ...pigment);
  }
  const buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(vertices), gl.STATIC_DRAW);
  for (let attribute = 0; attribute < 3; attribute++) {
    gl.enableVertexAttribArray(attribute);
    gl.vertexAttribPointer(attribute, 3, gl.FLOAT, false, 36, attribute * 12);
  }
  const camera = new PerspectiveCamera(39, canvas.width / canvas.height, .1, 60);
  camera.position.set(0,0,6.5);
  camera.updateMatrixWorld(true);
  const block = new Object3D();
  block.scale.set(1.08,.76,.66);
  gl.uniformMatrix4fv(gl.getUniformLocation(program,'projection'),false,camera.projectionMatrix.elements);
  gl.uniformMatrix4fv(gl.getUniformLocation(program,'view'),false,camera.matrixWorldInverse.elements);
  gl.viewport(0,0,canvas.width,canvas.height);
  gl.enable(gl.DEPTH_TEST);
  gl.disable(gl.DITHER);
  let turned = false;
  function draw() {
    block.rotation.set(.27, turned ? 1.2 : -.36,.04);
    block.updateMatrixWorld(true);
    gl.uniformMatrix4fv(gl.getUniformLocation(program,'model'),false,block.matrixWorld.elements);
    gl.clearColor(1,1,1,1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.drawArrays(gl.TRIANGLES,0,36);
    output.value = turned ? 'Turned' : 'Front';
  }
  button.addEventListener('click', () => { turned = !turned; draw(); });
  window.addEventListener('pagehide', () => {
    gl.deleteBuffer(buffer);
    gl.deleteProgram(program);
    gl.deleteShader(vertex);
    gl.deleteShader(fragment);
  }, { once: true });
  draw();
}

if (gl) {
  mount();
} else {
  output.value = 'WebGL2 unavailable';
  button.disabled = true;
}
