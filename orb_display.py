#!/usr/bin/env python3
"""
orb_display.py
--------------
Fullscreen pygame/OpenGL ES orb display for the Voice Orb project.
Connects to voice_orb_bridge WebSocket (ws://localhost:8765) for state/audio updates.
Replaces the Chromium kiosk canvas approach.

States received from bridge:
  idle, wake, listening, thinking, speaking, error
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass

import numpy as np
import pygame
from OpenGL import GL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("orb_display")

BRIDGE_URI = "ws://localhost:8765"

# Map bridge states to renderer states
STATE_MAP = {
    "idle": "idle",
    "wake": "listening",
    "listening": "listening",
    "thinking": "thinking",
    "speaking": "responding",
    "error": "idle",
}

# Particle density (1-10). Lower = fewer particles = faster on Pi Zero 2 W.
DENSITY = 8
ORBIT   = 6

# ─────────────────────────────────────────────────────────
# GLES Renderer (inlined from ha_ghost_assistant)
# ─────────────────────────────────────────────────────────

def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


@dataclass
class _ParticleField:
    positions: np.ndarray
    previous: np.ndarray
    seeds: np.ndarray
    ages: np.ndarray
    life: np.ndarray
    trail: np.ndarray


class _ShaderProgram:
    def __init__(self, vertex_src: str, fragment_src: str) -> None:
        self.program = GL.glCreateProgram()
        self._uniform_locs: dict[str, int] = {}
        self._attrib_locs: dict[str, int] = {}
        v = self._compile(GL.GL_VERTEX_SHADER, vertex_src)
        f = self._compile(GL.GL_FRAGMENT_SHADER, fragment_src)
        GL.glAttachShader(self.program, v)
        GL.glAttachShader(self.program, f)
        GL.glLinkProgram(self.program)
        if GL.glGetProgramiv(self.program, GL.GL_LINK_STATUS) != GL.GL_TRUE:
            raise RuntimeError(GL.glGetProgramInfoLog(self.program).decode())
        GL.glDeleteShader(v)
        GL.glDeleteShader(f)

    @staticmethod
    def _compile(shader_type: int, source: str) -> int:
        s = GL.glCreateShader(shader_type)
        GL.glShaderSource(s, source)
        GL.glCompileShader(s)
        if GL.glGetShaderiv(s, GL.GL_COMPILE_STATUS) != GL.GL_TRUE:
            raise RuntimeError(GL.glGetShaderInfoLog(s).decode())
        return s

    def use(self) -> None:
        GL.glUseProgram(self.program)

    def uniform(self, name: str) -> int:
        loc = self._uniform_locs.get(name)
        if loc is None:
            loc = GL.glGetUniformLocation(self.program, name)
            self._uniform_locs[name] = loc
        return loc

    def attrib(self, name: str) -> int:
        loc = self._attrib_locs.get(name)
        if loc is None:
            loc = GL.glGetAttribLocation(self.program, name)
            self._attrib_locs[name] = loc
        return loc


_BG_VERTEX = """
attribute vec2 a_pos;
varying vec2 v_uv;
void main() {
    v_uv = (a_pos + 1.0) * 0.5;
    gl_Position = vec4(a_pos, 0.0, 1.0);
}
"""

_BG_FRAGMENT = """
precision mediump float;
varying vec2 v_uv;
uniform vec2 u_resolution;
uniform vec2 u_center;
uniform float u_radius;
uniform float u_focus;
uniform float u_speak;
uniform float u_time;
uniform float u_env;
uniform float u_glow;
void main() {
    vec2 frag = v_uv * u_resolution;
    vec2 d = (frag - u_center) / max(u_radius, 1.0);
    float r = length(d);
    float angle = atan(d.y, d.x);

    // Animated surface wobble (stronger when active)
    float wobble = sin(angle * 6.0 + u_time * 1.8) * 0.022
                 + sin(angle * 11.0 - u_time * 2.7) * 0.011;
    float rw = r - wobble * (u_speak * 0.8 + u_focus * 0.3);

    // Primary ring at orb surface (use v*v instead of pow to avoid GLES undef for neg base)
    float rv = (rw - 0.93) * 5.5;
    float ring = exp(-rv * rv);

    // Angular brightness variation: rotating hotspots make it feel alive
    float angvar = 0.55 + 0.28 * sin(angle * 3.0 + u_time * 0.75)
                        + 0.17 * sin(angle * 5.0 - u_time * 1.2);
    angvar = max(0.08, angvar);
    ring *= angvar;

    // Outward-expanding pulse rings when speaking
    float t1 = fract(u_time * 0.38);
    float t2 = fract(u_time * 0.38 + 0.5);
    float p1v = (r - t1 * 0.92) * 10.0;
    float p2v = (r - t2 * 0.92) * 10.0;
    float pulse = (exp(-p1v * p1v) * (1.0 - t1)
                 + exp(-p2v * p2v) * (1.0 - t2))
                 * u_speak * 0.45;

    // Atmospheric halo just beyond ring
    float hv = max(0.0, rw - 0.82) * 2.0;
    float halo = exp(-hv * hv) * 0.45;

    // Subtle inner fill — mostly void, avoids the white blob
    float fill = exp(-r * r * 5.0) * (0.04 + 0.14 * u_speak + 0.06 * u_focus);

    // Colors: blue-violet base, shifts toward teal when speaking
    vec3 ring_col = mix(vec3(0.52, 0.30, 1.0), vec3(0.25, 0.70, 1.0), u_speak * 0.55);
    vec3 halo_col = mix(vec3(0.28, 0.10, 0.75), vec3(0.15, 0.45, 0.90), u_speak * 0.55);
    vec3 fill_col = mix(vec3(0.40, 0.20, 0.85), vec3(0.20, 0.65, 1.00), u_speak);

    float boost = 1.0 + u_speak * 0.9 + u_env * 0.5;
    vec3 color = ring_col * (ring + pulse) * boost
               + halo_col * halo
               + fill_col * fill;

    float fade = smoothstep(1.45, 0.60, r);
    color *= fade * u_glow;
    gl_FragColor = vec4(color, 1.0);
}
"""

_PARTICLE_VERTEX = """
attribute vec2 a_pos;
uniform vec2 u_resolution;
uniform float u_point_size;
void main() {
    vec2 clip = (a_pos / u_resolution) * 2.0 - 1.0;
    gl_Position = vec4(clip.x, -clip.y, 0.0, 1.0);
    gl_PointSize = u_point_size;
}
"""

_PARTICLE_FRAGMENT = """
precision mediump float;
uniform float u_alpha;
uniform float u_env;
void main() {
    vec2 c = gl_PointCoord - vec2(0.5);
    float r = length(c) * 2.0;
    float glow = exp(-r * r * 2.2);
    vec3 col = mix(vec3(0.65, 0.35, 1.0), vec3(0.48, 0.95, 1.0), u_env);
    gl_FragColor = vec4(col * glow, glow * u_alpha);
}
"""


class GLESRenderer:
    """OpenGL ES 2.0 orb renderer with GPU shaders and particle field."""

    def __init__(self, density: int = DENSITY, orbit: int = ORBIT) -> None:
        self._screen: pygame.Surface | None = None
        self._clock: pygame.time.Clock | None = None
        self._t0 = time.perf_counter()
        self._state: str = "idle"
        self._rms: float = 0.0
        self._env_fast: float = 0.0
        self._density = int(_clamp(float(density), 1, 10))
        self._orbit   = int(_clamp(float(orbit),   1, 10))
        self._field: _ParticleField | None = None
        self._bg_shader: _ShaderProgram | None = None
        self._particle_shader: _ShaderProgram | None = None
        self._quad_vbo: int | None = None
        self._particle_vbo: int | None = None
        self._particle_trail_vbo: int | None = None
        self._frame_index: int = 0
        self._glow_alpha: float = 0.0
        self._focus_smooth: float = 0.0
        self._speak_smooth: float = 0.0

    def set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            log.info("Orb state -> %s", state)

    def set_rms(self, rms: float) -> None:
        self._rms = float(rms)

    async def run(self, stop_event: asyncio.Event) -> None:
        pygame.init()
        pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MAJOR_VERSION, 2)
        pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MINOR_VERSION, 0)
        pygame.display.gl_set_attribute(
            pygame.GL_CONTEXT_PROFILE_MASK, pygame.GL_CONTEXT_PROFILE_ES
        )
        self._screen = pygame.display.set_mode(
            (0, 0), pygame.FULLSCREEN | pygame.OPENGL | pygame.DOUBLEBUF
        )
        pygame.display.set_caption("Voice Orb")
        self._clock = pygame.time.Clock()
        self._setup_gl()
        log.info("GLES renderer started  %dx%d", *self._screen.get_size())

        try:
            while not stop_event.is_set():
                for ev in pygame.event.get():
                    if ev.type == pygame.QUIT:
                        stop_event.set()
                    elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                        stop_event.set()
                self._draw_frame()
                await asyncio.sleep(0)   # yield to other tasks
        finally:
            pygame.display.quit()
            pygame.quit()
            log.info("GLES renderer stopped")

    # ── GL setup ─────────────────────────────────────────────────────────────

    def _setup_gl(self) -> None:
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFunc(GL.GL_ONE, GL.GL_ONE)
        GL.glClearColor(0.0, 0.0, 0.0, 1.0)

        self._bg_shader       = _ShaderProgram(_BG_VERTEX, _BG_FRAGMENT)
        self._particle_shader = _ShaderProgram(_PARTICLE_VERTEX, _PARTICLE_FRAGMENT)

        quad = np.array([-1.0, -1.0, 3.0, -1.0, -1.0, 3.0], dtype=np.float32)
        self._quad_vbo = GL.glGenBuffers(1)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._quad_vbo)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, quad.nbytes, quad, GL.GL_STATIC_DRAW)

        self._particle_vbo       = GL.glGenBuffers(1)
        self._particle_trail_vbo = GL.glGenBuffers(1)

    # ── Frame ────────────────────────────────────────────────────────────────

    def _draw_frame(self) -> None:
        if self._screen is None or self._clock is None:
            return
        w, h = self._screen.get_size()
        now  = time.perf_counter() - self._t0
        dt   = self._clock.tick(30) / 1000.0   # 30 fps cap (Pi Zero 2 W friendly)
        self._frame_index += 1

        rms = _clamp(self._rms, 0.0, 1.0)
        self._env_fast = (self._env_fast * 0.62) + (rms * 0.38)

        focus_target = 1.0 if self._state in ("listening", "thinking", "responding") else 0.0
        speak_target = 1.0 if self._state == "responding" else 0.0
        self._focus_smooth += (focus_target - self._focus_smooth) * min(dt * 4.0, 1.0)
        self._speak_smooth += (speak_target - self._speak_smooth) * min(dt * 1.5, 1.0)
        focus = self._focus_smooth
        speak = self._speak_smooth

        # Organic breathing when speaking: two overlapping sine waves
        breathe = 0.5 + 0.5 * math.sin(now * 3.2 + math.sin(now * 1.9) * 1.4)
        pulse = breathe * 0.22 * speak + 0.06 * self._env_fast

        base   = min(w, h) * 0.35
        radius = base * (1.0 + 0.06 * focus + 0.18 * speak + pulse)
        radius = float(_clamp(radius, 40.0, min(w, h) * 0.50))
        cx, cy = w * 0.5, h * 0.5

        is_idle = self._state == "idle"
        field = self._ensure_particles(radius, cx, cy, speak, is_idle)
        self._update_particles(field, radius, cx, cy, dt, now, is_idle)

        # Glow fades in when active, fades out when idle
        glow_target = 0.0 if self._state == "idle" else 1.0
        self._glow_alpha += (glow_target - self._glow_alpha) * min(dt * 3.0, 1.0)

        GL.glViewport(0, 0, w, h)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        if self._glow_alpha > 0.01:
            self._draw_background(w, h, radius, focus, speak, now, self._glow_alpha)
        self._draw_particles(field, w, h, radius, speak)
        pygame.display.flip()

    def _draw_background(
        self, w: int, h: int, radius: float, focus: float, speak: float, t: float,
        glow: float = 1.0,
    ) -> None:
        sh = self._bg_shader
        if sh is None or self._quad_vbo is None:
            return
        sh.use()
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._quad_vbo)
        pos_loc = sh.attrib("a_pos")
        GL.glEnableVertexAttribArray(pos_loc)
        GL.glVertexAttribPointer(pos_loc, 2, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        GL.glUniform2f(sh.uniform("u_resolution"), w, h)
        GL.glUniform2f(sh.uniform("u_center"), w * 0.5, h * 0.5)
        GL.glUniform1f(sh.uniform("u_radius"), radius)
        GL.glUniform1f(sh.uniform("u_focus"),  focus)
        GL.glUniform1f(sh.uniform("u_speak"),  speak)
        GL.glUniform1f(sh.uniform("u_time"),   t)
        GL.glUniform1f(sh.uniform("u_env"),    self._env_fast)
        GL.glUniform1f(sh.uniform("u_glow"),   glow)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)

    def _draw_particles(
        self, field: _ParticleField, w: int, h: int, radius: float, speak: float
    ) -> None:
        sh = self._particle_shader
        if sh is None or self._particle_vbo is None or self._particle_trail_vbo is None:
            return
        sh.use()
        GL.glUniform2f(sh.uniform("u_resolution"), w, h)
        GL.glUniform1f(sh.uniform("u_radius"),    radius)
        GL.glUniform1f(sh.uniform("u_env"),       self._env_fast)

        pos_loc = sh.attrib("a_pos")
        GL.glEnableVertexAttribArray(pos_loc)

        # Trails (skip when speaking to save perf)
        if speak <= 0.5:
            trail = field.trail
            trail[0::2] = field.previous
            trail[1::2] = field.positions
            GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._particle_trail_vbo)
            GL.glBufferData(GL.GL_ARRAY_BUFFER, trail.nbytes, trail, GL.GL_DYNAMIC_DRAW)
            GL.glVertexAttribPointer(pos_loc, 2, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
            GL.glUniform1f(sh.uniform("u_point_size"), 2.0)
            GL.glUniform1f(sh.uniform("u_alpha"),      0.12)
            GL.glDrawArrays(GL.GL_LINES, 0, trail.shape[0])

        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._particle_vbo)
        GL.glBufferData(
            GL.GL_ARRAY_BUFFER, field.positions.nbytes, field.positions, GL.GL_DYNAMIC_DRAW
        )
        GL.glVertexAttribPointer(pos_loc, 2, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        GL.glUniform1f(sh.uniform("u_point_size"), 4.8)
        GL.glUniform1f(sh.uniform("u_alpha"),      0.72)
        GL.glDrawArrays(GL.GL_POINTS, 0, field.positions.shape[0])

    def _ensure_particles(
        self, radius: float, cx: float, cy: float, speak: float, is_idle: bool = False
    ) -> _ParticleField:
        base_target = int(600 + (self._density - 1) * (3600 / 9))
        if speak > 0.5:
            scale = 0.18
        elif speak > 0.2:
            scale = 0.55
        else:
            scale = 1.0
        target = max(100, int(base_target * scale))

        if self._field is not None and self._field.positions.shape[0] == target:
            return self._field

        orbit_f = (self._orbit - 1) / 9.0
        ang  = np.random.uniform(0.0, math.tau, target).astype(np.float32)
        inner_mult = 0.25 if is_idle else 1.0
        inner = (0.25 + 0.50 * (1.0 - orbit_f)) * inner_mult
        outer = 0.95 + 0.70 * orbit_f
        rr   = radius * np.random.uniform(inner, outer, target).astype(np.float32)
        x    = cx + np.cos(ang) * rr
        y    = cy + np.sin(ang) * rr
        pos  = np.stack([x, y], axis=1).astype(np.float32)
        self._field = _ParticleField(
            positions=pos,
            previous=pos.copy(),
            seeds=np.random.uniform(0.0, 1000.0, target).astype(np.float32),
            ages=np.random.uniform(0.0, 3.0, target).astype(np.float32),
            life=np.random.uniform(1.6, 4.6, target).astype(np.float32),
            trail=np.empty((target * 2, 2), dtype=np.float32),
        )
        return self._field

    def _update_particles(
        self, field: _ParticleField, radius: float, cx: float, cy: float, dt: float, t: float,
        is_idle: bool = False,
    ) -> None:
        orbit_f    = (self._orbit - 1) / 9.0
        inner_mult = 0.25 if is_idle else 1.0
        inner_keep = radius * (0.28 + 0.46 * (1.0 - orbit_f)) * inner_mult
        outer_keep = radius * (1.02 + 0.65 * orbit_f)
        speed      = (26.0 + 90.0 * self._env_fast) * (0.68 + 0.65 * orbit_f)

        pos, prev, seeds, ages, life = (
            field.positions, field.previous, field.seeds, field.ages, field.life
        )
        prev[:] = pos
        ages += dt

        dx   = (pos[:, 0] - cx) / radius
        dy   = (pos[:, 1] - cy) / radius
        dist = np.sqrt(dx * dx + dy * dy) + 1e-6
        tx   = -dy / dist
        ty   =  dx / dist

        flow  = (np.sin(dx * 2.4 + t * 0.7 + seeds * 0.002)
                 + np.cos(dy * 2.0 - t * 0.6 + seeds * 0.0013))
        angle = flow * 1.4 + tx * 0.3
        mix   = 0.55 + 0.35 * orbit_f
        vx    = (np.cos(angle) * (1.0 - mix) + tx * mix) * speed
        vy    = (np.sin(angle) * (1.0 - mix) + ty * mix) * speed

        ax, ay = np.zeros_like(vx), np.zeros_like(vy)
        radial = dist * radius
        im = radial < inner_keep
        om = radial > outer_keep
        ip = (inner_keep - radial) / inner_keep
        op = (radial - outer_keep) / outer_keep
        ax[im] += (dx[im] / dist[im]) * speed * 0.65 * ip[im]
        ay[im] += (dy[im] / dist[im]) * speed * 0.65 * ip[im]
        ax[om] -= (dx[om] / dist[om]) * speed * 0.70 * op[om]
        ay[om] -= (dy[om] / dist[om]) * speed * 0.70 * op[om]

        pos[:, 0] += (vx + ax) * dt
        pos[:, 1] += (vy + ay) * dt

        reset = (ages > life) | (
            np.sqrt((pos[:, 0] - cx) ** 2 + (pos[:, 1] - cy) ** 2) > radius * 2.0
        )
        if np.any(reset):
            n   = int(reset.sum())
            ang = np.random.uniform(0.0, math.tau, n)
            rr  = radius * np.random.uniform(
                (0.25 + 0.50 * (1.0 - orbit_f)) * inner_mult,
                0.95 + 0.70 * orbit_f,
                n,
            )
            pos[reset, 0] = cx + np.cos(ang) * rr
            pos[reset, 1] = cy + np.sin(ang) * rr
            prev[reset]   = pos[reset]
            ages[reset]   = 0.0
            life[reset]   = np.random.uniform(1.6, 4.6, n)


# ─────────────────────────────────────────────────────────
# WebSocket client -- connects to voice_orb_bridge
# ─────────────────────────────────────────────────────────

async def ws_client(renderer: GLESRenderer, stop_event: asyncio.Event) -> None:
    import websockets

    while not stop_event.is_set():
        try:
            log.info("Connecting to bridge at %s ...", BRIDGE_URI)
            async with websockets.connect(BRIDGE_URI, ping_interval=20) as ws:
                log.info("Bridge connected")
                async for raw in ws:
                    if stop_event.is_set():
                        break
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if "state" in data:
                        renderer.set_state(STATE_MAP.get(data["state"], "idle"))
                    if "audio_level" in data:
                        renderer.set_rms(data["audio_level"])
        except Exception as exc:
            if not stop_event.is_set():
                log.warning("Bridge disconnected (%s) -- retry in 3 s", exc)
                await asyncio.sleep(3)


# ─────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────

async def _main() -> None:
    # Let SDL pick Wayland when running inside labwc session
    if "WAYLAND_DISPLAY" in os.environ and "SDL_VIDEODRIVER" not in os.environ:
        os.environ.setdefault("SDL_VIDEODRIVER", "wayland")

    stop_event = asyncio.Event()
    renderer   = GLESRenderer(density=DENSITY, orbit=ORBIT)

    await asyncio.gather(
        renderer.run(stop_event),
        ws_client(renderer, stop_event),
    )


if __name__ == "__main__":
    asyncio.run(_main())
