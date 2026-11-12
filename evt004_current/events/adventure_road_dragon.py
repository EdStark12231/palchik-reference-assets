#!/usr/bin/env python3
r"""PALCHIK — Adventure Road AI test bench using the live GameScene renderer.

Imports current game_scene.py and game_logic.py from --project-root at runtime.
No project source or save file is modified.

Run with:
    python adventure_road_event_test_v3.py --project-root C:\Users\PC\Desktop\project

Controls
--------
LMB              Set a flight destination anywhere in the play field
RMB              Land at the clicked point
1                Entrance animation
2                Roar animation
3                Free flight
4                Flying fire-breath attack
5                Land at current position
6                Ground attack
7                Hit reaction
8                Death / pixel disintegration
I                Toggle autonomous interface-destruction AI
T                Restore damaged PALCHIK UI elements and reset dragon
M                Toggle manual random waypoint flight
R                Reset to grounded idle
SPACE            Pause / resume
ESC              Quit
"""
from __future__ import annotations

import ast
import math
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import pygame
except ImportError as exc:  # Friendly message when run outside the game venv.
    raise SystemExit(
        "Pygame is required to run this visual test.\n"
        "Activate the PALCHIK virtual environment and run:\n"
        "    python -m pip install pygame\n"
        "Then run this file again."
    ) from exc


ROOT = Path(__file__).resolve().parent.parent
ASSET_DIR = ROOT / "assets" / "games_sprites" / "adventure_road"
FRAME_W = 96
FRAME_H = 96
WINDOW_W = 1120
WINDOW_H = 740
FPS = 60

# Classic Game Boy green palette. The sprite atlases use only these four colors.
GB_DARK = (15, 56, 15)
GB_MID_DARK = (48, 98, 48)
GB_LIGHT = (139, 172, 15)
GB_BRIGHT = (155, 188, 15)
GB_PALETTE = (GB_DARK, GB_MID_DARK, GB_LIGHT, GB_BRIGHT)

# UI/background shades are deliberately very dark so the four sprite shades read clearly.
BG = (13, 23, 15)
BG_GRID = (22, 39, 25)
PANEL = (19, 34, 21)
PANEL_BORDER = (48, 79, 43)
TEXT = (191, 211, 153)
TEXT_DIM = (112, 145, 91)

FIELD = pygame.Rect(28, 86, WINDOW_W - 56, WINDOW_H - 190)
DRAGON_DRAW_SIZE = 150
MAX_SPEED = 265.0
MAX_ACCEL = 620.0
TARGET_RADIUS = 18.0


# ---------------------------------------------------------------------------
# Animation and particle helpers
# ---------------------------------------------------------------------------

@dataclass
class Animation:
    frames: list[pygame.Surface]
    fps: float
    loop: bool = True

    @property
    def duration(self) -> float:
        return len(self.frames) / max(self.fps, 0.001)

    def frame(self, elapsed: float) -> pygame.Surface:
        if not self.frames:
            raise RuntimeError("Animation has no frames")
        index = int(max(0.0, elapsed) * self.fps)
        if self.loop:
            index %= len(self.frames)
        else:
            index = min(index, len(self.frames) - 1)
        return self.frames[index]

    def finished(self, elapsed: float) -> bool:
        return not self.loop and elapsed >= self.duration


@dataclass
class Particle:
    pos: pygame.Vector2
    vel: pygame.Vector2
    color: tuple[int, int, int]
    life: float
    max_life: float
    size: int = 3
    gravity: float = 0.0
    drag: float = 0.0
    kind: str = "pixel"

    def update(self, dt: float) -> None:
        self.life -= dt
        if self.gravity:
            self.vel.y += self.gravity * dt
        if self.drag:
            self.vel *= max(0.0, 1.0 - self.drag * dt)
        self.pos += self.vel * dt

    @property
    def alive(self) -> bool:
        return self.life > 0.0


# ---------------------------------------------------------------------------
# Asset loading
# ---------------------------------------------------------------------------

def load_atlas(filename: str, frame_count: int) -> list[pygame.Surface]:
    path = ASSET_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(f"Required sprite atlas is missing: {path}")
    atlas = pygame.image.load(str(path)).convert_alpha()
    expected = (FRAME_W * frame_count, FRAME_H)
    if atlas.get_size() != expected:
        raise ValueError(
            f"Unexpected dimensions for {filename}: {atlas.get_size()}, expected {expected}"
        )
    frames: list[pygame.Surface] = []
    for index in range(frame_count):
        rect = pygame.Rect(index * FRAME_W, 0, FRAME_W, FRAME_H)
        frames.append(atlas.subsurface(rect).copy())
    return frames


class DragonAssets:
    def __init__(self) -> None:
        self.flight = Animation(load_atlas("dragon_flight.png", 8), 9, True)
        self.ground_idle = Animation(load_atlas("dragon_ground_idle.png", 5), 5, True)
        self.roar = Animation(load_atlas("dragon_roar.png", 29), 12, False)
        self.ground_attack = Animation(load_atlas("dragon_ground_attack.png", 36), 12, False)


# ---------------------------------------------------------------------------
# Dragon controller / state machine
# ---------------------------------------------------------------------------

class Dragon:
    GROUNDED = "GROUND IDLE"
    APPEAR = "APPEAR"
    ROAR = "ROAR"
    FLIGHT = "FLIGHT"
    FLY_ATTACK = "FLY ATTACK"
    LANDING = "LANDING"
    GROUND_ATTACK = "GROUND ATTACK"
    HIT = "HIT"
    DEATH = "DEATH"

    AIR_STATES = {APPEAR, FLIGHT, FLY_ATTACK, LANDING}

    def __init__(self, assets: DragonAssets) -> None:
        self.assets = assets
        self.random = random.Random()
        self.particles: list[Particle] = []
        self.reset()

    def reset(self) -> None:
        self.state = self.GROUNDED
        self.state_time = 0.0
        self.idle_time = 0.0
        self.fly_time = 0.0
        self.pos = pygame.Vector2(FIELD.centerx, FIELD.centery + 12)
        self.velocity = pygame.Vector2(0, 0)
        self.target = self.pos.copy()
        self.heading = pygame.Vector2(1, 0)
        self.facing_left = False
        self.autopilot = False
        self.appear_target = self.pos.copy()
        self.appear_trail_timer = 0.0
        self.fire_timer = 0.0
        self.landing_target = self.pos.copy()
        self.landing_followup: Optional[str] = None
        self.hit_resume_state = self.FLIGHT
        self.hit_sprite: Optional[pygame.Surface] = None
        self.hit_push = pygame.Vector2(0, 0)
        self.death_elapsed = 0.0
        self.death_fragments: list[Particle] = []
        self.dead = False
        self.particles.clear()

    # ----- State transitions -------------------------------------------------

    def start_appearance(self, target: Optional[pygame.Vector2] = None) -> None:
        if self.dead:
            return
        margin = 100
        edge = self.random.choice(("left", "right", "top", "bottom"))
        if edge == "left":
            self.pos = pygame.Vector2(FIELD.left - margin, self.random.randint(FIELD.top + 40, FIELD.bottom - 40))
        elif edge == "right":
            self.pos = pygame.Vector2(FIELD.right + margin, self.random.randint(FIELD.top + 40, FIELD.bottom - 40))
        elif edge == "top":
            self.pos = pygame.Vector2(self.random.randint(FIELD.left + 40, FIELD.right - 40), FIELD.top - margin)
        else:
            self.pos = pygame.Vector2(self.random.randint(FIELD.left + 40, FIELD.right - 40), FIELD.bottom + margin)
        self.appear_target = self.clamp_to_field(target if target is not None else pygame.Vector2(FIELD.centerx, FIELD.centery))
        self.target = self.appear_target.copy()
        direction = self.appear_target - self.pos
        self.heading = direction.normalize() if direction.length_squared() else pygame.Vector2(1, 0)
        self.velocity = self.heading * 95
        self._set_state(self.APPEAR)
        self.appear_trail_timer = 0.0

    def start_roar(self) -> None:
        if self.dead:
            return
        if self.state in self.AIR_STATES or (self.state == self.HIT and self.hit_resume_state in self.AIR_STATES):
            self.start_landing(self.pos.copy(), followup=self.ROAR)
            return
        self.velocity.update(0, 0)
        self._set_state(self.ROAR)

    def start_flight(self, target: Optional[pygame.Vector2] = None) -> None:
        if self.dead:
            return
        if target is not None:
            self.target = self.clamp_to_field(target)
            self.autopilot = False
        elif self.target.distance_to(self.pos) < TARGET_RADIUS:
            self.target = self.random_waypoint()
        self._set_state(self.FLIGHT)
        if self.velocity.length() < 22:
            direction = self.target - self.pos
            if direction.length_squared() > 1:
                self.velocity = direction.normalize() * 65

    def start_flying_attack(self) -> None:
        if self.dead:
            return
        if self.state not in self.AIR_STATES and not (self.state == self.HIT and self.hit_resume_state in self.AIR_STATES):
            self.start_flight(self.random_waypoint())
        self._set_state(self.FLY_ATTACK)
        self.fire_timer = 0.0

    def start_landing(self, target: Optional[pygame.Vector2] = None, followup: Optional[str] = None) -> None:
        if self.dead:
            return
        if target is not None:
            self.landing_target = self.clamp_to_field(target)
        else:
            self.landing_target = self.pos.copy()
        self.target = self.landing_target.copy()
        self.landing_followup = followup
        self._set_state(self.LANDING)

    def start_ground_attack(self) -> None:
        if self.dead:
            return
        if self.state in self.AIR_STATES or (self.state == self.HIT and self.hit_resume_state in self.AIR_STATES):
            self.start_landing(self.pos.copy(), followup=self.GROUND_ATTACK)
            return
        self.velocity.update(0, 0)
        self._set_state(self.GROUND_ATTACK)
        self.fire_timer = 0.0

    def start_hit(self) -> None:
        if self.dead or self.state == self.DEATH:
            return
        self.hit_resume_state = self.state if self.state != self.HIT else self.hit_resume_state
        self.hit_sprite = self.current_frame().copy()
        away = -self.heading if self.heading.length_squared() else pygame.Vector2(-1, 0)
        self.hit_push = away * 115
        self.velocity += self.hit_push * 0.32
        self._set_state(self.HIT)
        self._burst_sparks(self.pos, 18)

    def start_death(self) -> None:
        if self.dead:
            return
        sprite = self.transformed_sprite(for_death=True)
        self.death_fragments.clear()
        # Sample visible pixels from the already transformed sprite, preserving its palette.
        for y in range(0, sprite.get_height(), 4):
            for x in range(0, sprite.get_width(), 4):
                color = sprite.get_at((x, y))
                if color.a < 100:
                    continue
                local = pygame.Vector2(
                    x - sprite.get_width() / 2,
                    y - sprite.get_height() / 2,
                )
                radial = local.normalize() if local.length_squared() > 1 else pygame.Vector2(0, -1)
                radial.rotate_ip(self.random.uniform(-55, 55))
                speed = self.random.uniform(45, 210)
                self.death_fragments.append(
                    Particle(
                        self.pos + local,
                        radial * speed + pygame.Vector2(self.random.uniform(-24, 24), -self.random.uniform(10, 95)),
                        (color.r, color.g, color.b),
                        self.random.uniform(0.75, 1.65),
                        1.65,
                        size=self.random.choice((2, 3, 4)),
                        gravity=210,
                        drag=0.12,
                        kind="debris",
                    )
                )
        self.particles.clear()
        self._set_state(self.DEATH)
        self.dead = True
        self.death_elapsed = 0.0

    def _set_state(self, new_state: str) -> None:
        self.state = new_state
        self.state_time = 0.0
        if new_state in self.AIR_STATES:
            # The flight animation has its own clock and doesn't restart on every waypoint.
            pass

    # ----- Navigation --------------------------------------------------------

    @staticmethod
    def clamp_to_field(point: pygame.Vector2) -> pygame.Vector2:
        return pygame.Vector2(
            max(FIELD.left + 74, min(FIELD.right - 74, point.x)),
            max(FIELD.top + 68, min(FIELD.bottom - 68, point.y)),
        )

    def random_waypoint(self) -> pygame.Vector2:
        return pygame.Vector2(
            self.random.randint(FIELD.left + 90, FIELD.right - 90),
            self.random.randint(FIELD.top + 80, FIELD.bottom - 80),
        )

    def set_destination(self, point: pygame.Vector2) -> None:
        if self.dead:
            return
        point = self.clamp_to_field(point)
        if self.state == self.LANDING:
            self.landing_target = point
            self.target = point.copy()
            return
        self.autopilot = False
        self.start_flight(point)

    def toggle_autopilot(self) -> None:
        self.autopilot = not self.autopilot
        if self.autopilot and self.state not in self.AIR_STATES and self.state != self.HIT:
            self.start_flight(self.random_waypoint())
        elif self.autopilot and self.state == self.FLIGHT and self.target.distance_to(self.pos) < TARGET_RADIUS:
            self.target = self.random_waypoint()

    def _steer_to(self, target: pygame.Vector2, dt: float, max_speed: float = MAX_SPEED, constrain: bool = True) -> None:
        delta = target - self.pos
        distance = delta.length()
        if distance > 1:
            direction = delta / distance
            desired_speed = min(max_speed, math.sqrt(max(0.0, 2 * MAX_ACCEL * distance)))
            desired_velocity = direction * desired_speed
        else:
            desired_velocity = pygame.Vector2(0, 0)

        change = desired_velocity - self.velocity
        max_change = MAX_ACCEL * dt
        if change.length() > max_change > 0:
            change.scale_to_length(max_change)
        self.velocity += change
        self.pos += self.velocity * dt

        # Keep normal flight inside the play area. Entrance is allowed to begin
        # outside the field so the dragon genuinely flies into view from an edge.
        if constrain:
            margin = 42
            if self.pos.x < FIELD.left + margin:
                self.pos.x = FIELD.left + margin
                self.velocity.x = abs(self.velocity.x) * 0.35
            elif self.pos.x > FIELD.right - margin:
                self.pos.x = FIELD.right - margin
                self.velocity.x = -abs(self.velocity.x) * 0.35
            if self.pos.y < FIELD.top + margin:
                self.pos.y = FIELD.top + margin
                self.velocity.y = abs(self.velocity.y) * 0.35
            elif self.pos.y > FIELD.bottom - margin:
                self.pos.y = FIELD.bottom - margin
                self.velocity.y = -abs(self.velocity.y) * 0.35

        if self.velocity.length_squared() > 64:
            self.heading = self.velocity.normalize()
            if abs(self.velocity.x) > 11:
                self.facing_left = self.velocity.x < 0
        elif delta.length_squared() > 1:
            self.heading = delta.normalize()
            if abs(delta.x) > 11:
                self.facing_left = delta.x < 0

    # ----- Current animation frame ------------------------------------------

    def current_frame(self) -> pygame.Surface:
        if self.state == self.GROUNDED:
            return self.assets.ground_idle.frame(self.idle_time)
        if self.state == self.ROAR:
            return self.assets.roar.frame(self.state_time)
        if self.state == self.GROUND_ATTACK:
            return self.assets.ground_attack.frame(self.state_time)
        if self.state == self.HIT and self.hit_sprite is not None:
            return self.hit_sprite
        return self.assets.flight.frame(self.fly_time)

    def transformed_sprite(self, for_death: bool = False) -> pygame.Surface:
        frame = self.current_frame().copy()
        sprite = pygame.transform.scale(frame, (DRAGON_DRAW_SIZE, DRAGON_DRAW_SIZE))
        if self.facing_left:
            sprite = pygame.transform.flip(sprite, True, False)
        # Convert movement vector to sprite rotation. This supports all 360 degrees,
        # not just left/right facing.
        dx, dy = self.heading.x, self.heading.y
        theta = math.degrees(math.atan2(dy, dx))
        angle = (180.0 - theta) if self.facing_left else -theta
        if abs(angle) > 0.5:
            sprite = pygame.transform.rotate(sprite, angle)
        if not for_death and self.state == self.HIT and int(self.state_time * 18) % 2 == 0:
            sprite = sprite.copy()
            sprite.fill((44, 62, 44), special_flags=pygame.BLEND_RGB_ADD)
        return sprite

    # ----- Effects -----------------------------------------------------------

    def _burst_sparks(self, origin: pygame.Vector2, amount: int) -> None:
        for _ in range(amount):
            angle = self.random.uniform(0, math.tau)
            speed = self.random.uniform(45, 190)
            vel = pygame.Vector2(math.cos(angle), math.sin(angle)) * speed
            self.particles.append(
                Particle(
                    origin + pygame.Vector2(self.random.uniform(-13, 13), self.random.uniform(-13, 13)),
                    vel,
                    self.random.choice((GB_MID_DARK, GB_LIGHT, GB_BRIGHT)),
                    self.random.uniform(0.22, 0.62),
                    0.62,
                    size=self.random.choice((2, 3, 4)),
                    gravity=65,
                    drag=0.7,
                    kind="spark",
                )
            )

    def _spawn_fire(self, count: int = 4) -> None:
        forward = self.heading.normalize() if self.heading.length_squared() else pygame.Vector2(1, 0)
        up = pygame.Vector2(forward.y, -forward.x)
        muzzle = self.pos + forward * 48 + up * 10
        for _ in range(count):
            spread = self.random.uniform(-0.24, 0.24)
            direction = forward.rotate(math.degrees(spread))
            speed = self.random.uniform(150, 255)
            self.particles.append(
                Particle(
                    muzzle + direction * self.random.uniform(0, 13),
                    direction * speed + up * self.random.uniform(-18, 18),
                    self.random.choice((GB_MID_DARK, GB_LIGHT, GB_BRIGHT)),
                    self.random.uniform(0.20, 0.48),
                    0.48,
                    size=self.random.choice((3, 4, 5, 6)),
                    gravity=-8,
                    drag=0.18,
                    kind="fire",
                )
            )

    def _spawn_landing_dust(self) -> None:
        for _ in range(24):
            side = self.random.choice((-1, 1))
            vel = pygame.Vector2(side * self.random.uniform(25, 115), self.random.uniform(-65, -8))
            self.particles.append(
                Particle(
                    self.pos + pygame.Vector2(self.random.uniform(-28, 28), 24),
                    vel,
                    self.random.choice((GB_MID_DARK, GB_LIGHT)),
                    self.random.uniform(0.28, 0.62),
                    0.62,
                    size=self.random.choice((2, 3, 4)),
                    gravity=120,
                    drag=0.45,
                    kind="dust",
                )
            )

    # ----- Update ------------------------------------------------------------

    def update(self, dt: float) -> None:
        dt = min(dt, 1 / 20)  # Avoid large physics jumps after dragging the window.
        self.state_time += dt
        self.idle_time += dt
        self.fly_time += dt

        for particle in self.particles:
            particle.update(dt)
        self.particles = [p for p in self.particles if p.alive]
        for particle in self.death_fragments:
            particle.update(dt)
        self.death_fragments = [p for p in self.death_fragments if p.alive]

        if self.state == self.APPEAR:
            self._steer_to(self.appear_target, dt, max_speed=310, constrain=False)
            self.appear_trail_timer += dt
            if self.appear_trail_timer >= 0.055:
                self.appear_trail_timer = 0.0
                self._burst_sparks(self.pos - self.heading * 39, 2)
            if self.pos.distance_to(self.appear_target) < 23 and self.velocity.length() < 90:
                self._set_state(self.FLIGHT)
                self.target = self.random_waypoint() if self.autopilot else self.pos.copy()
                self.velocity *= 0.35

        elif self.state == self.FLIGHT:
            self._update_flight_movement(dt)

        elif self.state == self.FLY_ATTACK:
            self._update_flight_movement(dt)
            self.fire_timer += dt
            if 0.24 <= self.state_time <= 1.22 and self.fire_timer >= 0.045:
                self.fire_timer = 0.0
                self._spawn_fire(4)
            if self.state_time >= 1.65:
                self._set_state(self.FLIGHT)

        elif self.state == self.LANDING:
            self._steer_to(self.landing_target, dt, max_speed=MAX_SPEED * 0.68)
            if self.pos.distance_to(self.landing_target) < 15 and self.velocity.length() < 48:
                self.pos = self.landing_target.copy()
                self.velocity *= 0.25
                if self.state_time > 0.55:
                    self._finish_landing()
            elif self.state_time > 2.8:
                self.pos = self.landing_target.copy()
                self.velocity.update(0, 0)
                self._finish_landing()

        elif self.state == self.ROAR:
            if self.assets.roar.finished(self.state_time):
                self._set_state(self.GROUNDED)

        elif self.state == self.GROUND_ATTACK:
            # Fire is a separate effect layer, emitted while the jaw is open.
            self.fire_timer += dt
            if 0.42 <= self.state_time <= 2.05 and self.fire_timer >= 0.055:
                self.fire_timer = 0.0
                self._spawn_fire(3)
            if self.assets.ground_attack.finished(self.state_time):
                self._set_state(self.GROUNDED)

        elif self.state == self.HIT:
            self.pos += self.hit_push * dt * max(0.0, 1.0 - self.state_time / 0.46)
            if self.state_time >= 0.46:
                self._set_state(self.hit_resume_state)
                self.hit_sprite = None
                self.hit_push.update(0, 0)

        elif self.state == self.DEATH:
            self.death_elapsed += dt

        # Keep orientation stable even while hovering.
        if self.state in (self.FLIGHT, self.FLY_ATTACK, self.APPEAR, self.LANDING) and self.velocity.length_squared() > 25:
            self.heading = self.velocity.normalize()
            if abs(self.velocity.x) > 11:
                self.facing_left = self.velocity.x < 0

    def _update_flight_movement(self, dt: float) -> None:
        self._steer_to(self.target, dt)
        if self.pos.distance_to(self.target) < TARGET_RADIUS and self.velocity.length() < 40:
            self.velocity *= 0.72
            if self.autopilot:
                self.target = self.random_waypoint()

    def _finish_landing(self) -> None:
        self._spawn_landing_dust()
        followup = self.landing_followup
        self.landing_followup = None
        self.heading = pygame.Vector2(-1, 0) if self.facing_left else pygame.Vector2(1, 0)
        self.velocity.update(0, 0)
        self._set_state(self.GROUNDED)
        if followup == self.ROAR:
            self.start_roar()
        elif followup == self.GROUND_ATTACK:
            self.start_ground_attack()

    # ----- Drawing -----------------------------------------------------------

    def draw(self, surface: pygame.Surface) -> None:
        if self.state != self.DEATH:
            sprite = self.transformed_sprite()
            # Entrance eases in through pixel-spark opacity rather than a scale pop.
            if self.state == self.APPEAR:
                alpha = int(255 * min(1.0, self.state_time / 0.32))
                sprite.set_alpha(alpha)
            if self.state == self.LANDING:
                # Slight downward settle during the last part of the landing.
                progress = min(1.0, self.state_time / 1.1)
                settle_y = math.sin(progress * math.pi) * 3.0
            else:
                settle_y = 0.0
            hover_y = math.sin(self.fly_time * 4.0) * 2.2 if self.state in self.AIR_STATES else 0.0
            rect = sprite.get_rect(center=(round(self.pos.x), round(self.pos.y + hover_y + settle_y)))
            surface.blit(sprite, rect)

        for particle in self.particles:
            self._draw_particle(surface, particle)
        for particle in self.death_fragments:
            self._draw_particle(surface, particle)

    @staticmethod
    def _draw_particle(surface: pygame.Surface, particle: Particle) -> None:
        life_ratio = max(0.0, min(1.0, particle.life / max(particle.max_life, 0.001)))
        size = max(1, int(particle.size * (0.55 + 0.45 * life_ratio)))
        rect = pygame.Rect(round(particle.pos.x), round(particle.pos.y), size, size)
        pygame.draw.rect(surface, particle.color, rect)
        if particle.kind == "fire" and size >= 4:
            inner = pygame.Rect(rect.x + size // 3, rect.y + size // 3, max(1, size // 3), max(1, size // 3))
            pygame.draw.rect(surface, GB_BRIGHT, inner)



# ---------------------------------------------------------------------------
# Targets extracted from the REAL PALCHIK GameControllerPygame render pass
# ---------------------------------------------------------------------------

@dataclass
class InterfaceTarget:
    key: str
    label: str
    rect: pygame.Rect
    kind: str
    priority: int
    attack_mode: str
    max_hp: int
    hp: int
    hit_flash: float = 0.0
    shake_time: float = 0.0
    fragments: list[Particle] = field(default_factory=list)
    intact_surface: pygame.Surface | None = field(default=None, repr=False)
    debris_shards: list[dict] = field(default_factory=list, repr=False)
    debris_initialized: bool = False
    destroyed: bool = False
    visible: bool = True

    @property
    def center(self) -> pygame.Vector2:
        return pygame.Vector2(self.rect.center)

    @property
    def radius(self) -> float:
        return max(12.0, math.hypot(self.rect.width, self.rect.height) * 0.5)

    def take_damage(self, rng: random.Random) -> bool:
        if self.destroyed or not self.visible:
            return False
        self.hp = max(0, self.hp - 1)
        self.hit_flash = 0.22
        self.shake_time = 0.24
        count = 24 if self.hp == 0 else 9
        for _ in range(count):
            pos = pygame.Vector2(
                rng.uniform(self.rect.left, self.rect.right),
                rng.uniform(self.rect.top, self.rect.bottom),
            )
            angle = rng.uniform(0.0, math.tau)
            speed = rng.uniform(55.0, 185.0 if self.hp == 0 else 115.0)
            self.fragments.append(Particle(
                pos, pygame.Vector2(math.cos(angle), math.sin(angle)) * speed,
                rng.choice((GB_MID_DARK, GB_LIGHT, GB_BRIGHT)),
                rng.uniform(0.30, 0.85), 0.85,
                size=rng.choice((2, 3, 4)), gravity=105.0, drag=0.32,
                kind='ui_debris',
            ))
        if self.hp == 0:
            self.destroyed = True
        return self.destroyed

    def update(self, dt: float) -> None:
        self.hit_flash = max(0.0, self.hit_flash - dt)
        self.shake_time = max(0.0, self.shake_time - dt)
        for p in self.fragments:
            p.update(dt)
        self.fragments = [p for p in self.fragments if p.alive]
        # EVT_004 destruction debris physics.
        alive_shards = []
        for shard in self.debris_shards:
            shard["life"] -= dt
            shard["vy"] += 235.0 * dt
            shard["vx"] *= max(0.0, 1.0 - 0.35 * dt)
            shard["x"] += shard["vx"] * dt
            shard["y"] += shard["vy"] * dt
            if shard["life"] > 0.0:
                alive_shards.append(shard)
        self.debris_shards = alive_shards

    def draw_damage(self, surface: pygame.Surface, background: pygame.Surface) -> None:
        rect = self.rect.clip(surface.get_rect())

        if rect.width <= 0 or rect.height <= 0:
            return

        if not self.visible:
            for particle in self.fragments:
                Dragon._draw_particle(surface, particle)
            return

        # Capture the original interface artwork before it is damaged.
        if self.intact_surface is None:
            raw = surface.subsurface(rect).copy()
            base = background.subsurface(rect)
            cutout = pygame.Surface(rect.size, pygame.SRCALPHA)

            for py in range(rect.height):
                for px in range(rect.width):
                    color = raw.get_at((px, py))
                    under = base.get_at((px, py))
                    delta = (
                        abs(color.r - under.r)
                        + abs(color.g - under.g)
                        + abs(color.b - under.b)
                    )
                    if delta > 22:
                        cutout.set_at((px, py), color)

            self.intact_surface = cutout

        image = self.intact_surface

        if self.destroyed:
            # Remove the actual UI element using the clean scene backdrop.
            surface.blit(background, rect.topleft, rect)

            if not self.debris_initialized:
                self.debris_initialized = True
                rng = random.Random(sum(ord(ch) for ch in self.key) + 407)
                step = max(5, int(max(rect.width, rect.height) / 17))

                for y in range(0, image.get_height(), step):
                    for x in range(0, image.get_width(), step):
                        width = min(step, image.get_width() - x)
                        height = min(step, image.get_height() - y)

                        piece = image.subsurface(
                            pygame.Rect(x, y, width, height)
                        ).copy()

                        has_pixels = False
                        for py in range(height):
                            for px in range(width):
                                if piece.get_at((px, py)).a > 0:
                                    has_pixels = True
                                    break
                            if has_pixels:
                                break

                        if not has_pixels:
                            continue

                        life = rng.uniform(0.48, 0.95)
                        self.debris_shards.append({
                            "image": piece,
                            "x": float(rect.left + x),
                            "y": float(rect.top + y),
                            "vx": rng.uniform(-145.0, 145.0),
                            "vy": rng.uniform(-205.0, -35.0),
                            "life": life,
                            "max_life": life,
                        })

            if self.hit_flash > 0.0:
                scorch = pygame.Surface(rect.size, pygame.SRCALPHA)
                alpha = int(95 * self.hit_flash / 0.22)
                scorch.fill((8, 16, 8, alpha))
                surface.blit(scorch, rect.topleft)

            for shard in self.debris_shards:
                alpha = max(
                    0,
                    min(255, int(255 * shard["life"] / shard["max_life"]))
                )
                piece = shard["image"].copy()
                piece.set_alpha(alpha)
                surface.blit(
                    piece,
                    (round(shard["x"]), round(shard["y"]))
                )

            for particle in self.fragments:
                Dragon._draw_particle(surface, particle)
            return

        draw_rect = rect.copy()

        # Shake the original UI artwork while restoring its previous position.
        if self.shake_time > 0.0:
            elapsed = 0.24 - self.shake_time
            amplitude = 2.0 + 3.0 * (self.shake_time / 0.24)
            offset_x = round(math.sin(elapsed * 92.0) * amplitude)
            offset_y = round(math.cos(elapsed * 78.0) * amplitude * 0.45)

            surface.blit(background, rect.topleft, rect)
            draw_rect.x += offset_x
            draw_rect.y += offset_y
            surface.blit(image, draw_rect.topleft)

        # Progressive cracks.
        if self.hp < self.max_hp:
            inset = draw_rect.inflate(-4, -4)
            if inset.width > 4 and inset.height > 4:
                seed = sum(ord(ch) for ch in self.key)
                rng = random.Random(seed + self.max_hp - self.hp)
                crack_color = (
                    GB_BRIGHT if self.hit_flash > 0.0 else GB_MID_DARK
                )

                for _ in range(self.max_hp - self.hp):
                    x = rng.randint(
                        inset.left + 2, max(inset.left + 2, inset.right - 2)
                    )
                    y = rng.randint(
                        inset.top + 2, max(inset.top + 2, inset.bottom - 2)
                    )
                    points = [(x, y)]

                    for _step in range(4):
                        x += rng.randint(-10, 10)
                        y += rng.randint(-8, 9)
                        points.append((
                            max(inset.left, min(inset.right, x)),
                            max(inset.top, min(inset.bottom, y)),
                        ))

                    pygame.draw.lines(
                        surface, crack_color, False, points, 2
                    )

        # Impact flash.
        if self.hit_flash > 0.0:
            flash = pygame.Surface(image.get_size(), pygame.SRCALPHA)
            alpha = int(82 * self.hit_flash / 0.22)
            flash.fill((190, 225, 40, alpha))
            surface.blit(flash, draw_rect.topleft)

        for particle in self.fragments:
            Dragon._draw_particle(surface, particle)


class DragonAI:
    """Small priority-based state machine that hunts and destroys UI targets."""
    SELECT = "SELECT TARGET"
    APPROACH_AIR = "APPROACH / AIR"
    AIR_ATTACK = "AIR ATTACK"
    APPROACH_GROUND = "APPROACH / GROUND"
    LANDING = "LANDING FOR ATTACK"
    GROUND_ATTACK = "GROUND ATTACK"
    COOLDOWN = "REASSESS"
    COMPLETE = "INTERFACE DESTROYED"

    def __init__(self, dragon: Dragon, targets: list[InterfaceTarget]) -> None:
        self.dragon = dragon
        self.targets = targets
        self.rng = random.Random(4107)
        self.enabled = False
        self.phase = self.SELECT
        self.current_target: Optional[InterfaceTarget] = None
        self.approach_point = pygame.Vector2(FIELD.center)
        self.phase_time = 0.0
        self.cooldown = 0.0
        self.impact_applied = False
        self.last_decision = "AI DISABLED"
        self.total_hits = 0
        self.destroyed_count = 0
        self.cursor_position: pygame.Vector2 | None = None
        self.cursor_mode = False
        self.cursor_attack_cooldown = 0.0
        self.cursor_detour_timer = 0.0
        self.cursor_detour_cooldown = 0.0
        self.cursor_interest_timer = 0.0

    def reset(self, targets: Optional[list[InterfaceTarget]] = None) -> None:
        if targets is not None:
            self.targets = targets
        self.enabled = False
        self.phase = self.SELECT
        self.current_target = None
        self.phase_time = 0.0
        self.cooldown = 0.0
        self.impact_applied = False
        self.last_decision = "AI DISABLED"
        self.total_hits = 0
        self.destroyed_count = 0
        self.cursor_interest_timer = 0.0
        self.cursor_mode = False
        self.cursor_detour_timer = 0.0

    def set_cursor_position(self, position: pygame.Vector2 | None) -> None:
        """Set a non-destructible cursor pursuit point."""
        self.cursor_position = (
            position.copy() if position is not None else None
        )

    def on_dragon_clicked(self) -> None:
        """Immediately provoke and renew cursor pursuit after a confirmed click."""
        self.cursor_interest_timer = 2.0
        self.cursor_detour_timer = 2.0
        self.cursor_detour_cooldown = 0.0
        self.cursor_mode = True
        self.last_decision = "PLAYER-PROVOKED CURSOR HUNT"

    def enable(self) -> None:
        if self.dragon.state == self.dragon.DEATH:
            return
        self.enabled = True
        self.phase = self.SELECT
        self.phase_time = 0.0
        self.current_target = None
        self.cooldown = 0.0
        self.impact_applied = False
        self.dragon.autopilot = False
        self.last_decision = "AUTONOMOUS HUNT ENABLED"

    def disable(self) -> None:
        self.enabled = False
        self.dragon.autopilot = False
        self.current_target = None
        self.cursor_mode = False
        self.cursor_interest_timer = 0.0
        self.cursor_detour_timer = 0.0
        self.phase = self.SELECT
        self.phase_time = 0.0
        self.last_decision = "MANUAL CONTROL"

    def toggle(self) -> None:
        if self.enabled:
            self.disable()
        else:
            self.enable()

    def _alive_targets(self) -> list[InterfaceTarget]:
        return [target for target in self.targets if target.visible and not target.destroyed and target.hp > 0]

    def _select_target(self) -> Optional[InterfaceTarget]:
        candidates = self._alive_targets()
        if not candidates:
            return None

        # Priority dominates; distance breaks ties so the dragon avoids needless travel.
        def score(target: InterfaceTarget) -> float:
            distance = self.dragon.pos.distance_to(target.center)
            damage_bonus = (target.max_hp - target.hp) * 7.0
            return target.priority * 100.0 + damage_bonus - distance * 0.055 + self.rng.random() * 0.25

        return max(candidates, key=score)

    def _calculate_approach_point(self, target: InterfaceTarget) -> pygame.Vector2:
        to_target = target.center - self.dragon.pos
        if to_target.length_squared() < 4.0:
            direction = pygame.Vector2(self.rng.choice((-1.0, 1.0)), self.rng.uniform(-0.7, 0.7))
            if direction.length_squared() < 0.01:
                direction = pygame.Vector2(1, 0)
            direction = direction.normalize()
        else:
            direction = to_target.normalize()
        stand_off = target.radius + (78.0 if target.attack_mode == "air" else 84.0)
        point = target.center - direction * stand_off
        return self.dragon.clamp_to_field(point)

    def _begin_next_target(self) -> None:
        target = self._select_target()
        if target is None:
            self.current_target = None
            self.phase = self.COMPLETE
            self.phase_time = 0.0
            self.last_decision = "ALL INTERFACE ELEMENTS DESTROYED"
            self.dragon.autopilot = True
            if self.dragon.state != self.dragon.FLIGHT:
                self.dragon.start_flight(self.dragon.random_waypoint())
            elif self.dragon.target.distance_to(self.dragon.pos) < TARGET_RADIUS:
                self.dragon.target = self.dragon.random_waypoint()
            return

        self.current_target = target
        self.approach_point = self._calculate_approach_point(target)
        self.phase_time = 0.0
        self.impact_applied = False
        self.dragon.autopilot = False
        self.dragon.start_flight(self.approach_point)
        if target.attack_mode == "ground":
            self.phase = self.APPROACH_GROUND
            self.last_decision = f"TARGET: {target.label} / GROUND ATTACK"
        else:
            self.phase = self.APPROACH_AIR
            self.last_decision = f"TARGET: {target.label} / FIRE BREATH"

    def _face_target(self) -> None:
        if self.current_target is None:
            return
        direction = self.current_target.center - self.dragon.pos
        if direction.length_squared() > 1.0:
            self.dragon.heading = direction.normalize()
            if abs(direction.x) > 1.0:
                self.dragon.facing_left = direction.x < 0

    def _apply_target_hit(self) -> None:
        target = self.current_target
        if target is None or target.destroyed or self.impact_applied:
            return
        was_destroyed = target.take_damage(self.rng)
        self.impact_applied = True
        self.total_hits += 1
        self.last_decision = (
            f"DESTROYED: {target.label}" if was_destroyed
            else f"HIT: {target.label} / HP {target.hp}/{target.max_hp}"
        )
        if was_destroyed:
            self.destroyed_count += 1

    def update(self, dt: float) -> None:
        if not self.enabled or self.phase == self.COMPLETE:
            return
        if self.dragon.state == self.dragon.DEATH:
            self.disable()
            return
        if self.dragon.state == self.dragon.HIT:
            return

        self.cursor_attack_cooldown = max(
            0.0, self.cursor_attack_cooldown - dt
        )
        self.cursor_detour_cooldown = max(
            0.0, self.cursor_detour_cooldown - dt
        )

        cursor = self.cursor_position

        # Clicks on the dragon renew this time-limited cursor pursuit.
        if self.cursor_mode:
            self.cursor_interest_timer = max(
                0.0, self.cursor_interest_timer - dt
            )
            self.cursor_detour_timer = self.cursor_interest_timer

            if cursor is None or self.cursor_interest_timer <= 0.0:
                self.cursor_mode = False
                self.cursor_detour_timer = 0.0
                self.cursor_interest_timer = 0.0
                self.cursor_detour_cooldown = 0.0
                self.phase = self.SELECT
                self.phase_time = 0.0
                self.last_decision = (
                    "CURSOR INTEREST LOST / RESUMING INTERFACE HUNT"
                )
            else:
                distance = self.dragon.pos.distance_to(cursor)

                if distance > 54.0:
                    if (
                        self.dragon.state != self.dragon.FLIGHT
                        or self.dragon.target.distance_to(cursor) > 36.0
                    ):
                        self.dragon.start_flight(cursor)
                else:
                    direction = cursor - self.dragon.pos
                    if direction.length_squared() > 1.0:
                        self.dragon.heading = direction.normalize()
                        self.dragon.facing_left = direction.x < 0

                    # Cursor is not an InterfaceTarget and receives no damage.
                    # Keep pursuit active after each visual attack.
                    if (
                        self.cursor_attack_cooldown <= 0.0
                        and self.dragon.state != self.dragon.FLY_ATTACK
                    ):
                        self.dragon.start_flying_attack()
                        self.cursor_attack_cooldown = 1.5
                        self.last_decision = "ATTACKING PLAYER CURSOR"
                return

        # Continue the normal DragonAI state machine.
        self.phase_time += dt
        target = self.current_target
        if target is not None and (target.destroyed or not target.visible) and self.phase not in (self.AIR_ATTACK, self.GROUND_ATTACK):
            self.current_target = None
            self.phase = self.COOLDOWN
            self.phase_time = 0.0
            self.cooldown = 0.2
            return

        if self.phase == self.SELECT:
            self._begin_next_target()
            return

        if self.phase == self.COOLDOWN:
            self.cooldown -= dt
            if self.cooldown <= 0.0:
                self.phase = self.SELECT
                self.phase_time = 0.0
            return

        if self.phase == self.APPROACH_AIR:
            if target is None:
                self.phase = self.SELECT
                return
            distance = self.dragon.pos.distance_to(self.approach_point)
            if (distance <= 25.0 and self.dragon.velocity.length() <= 72.0) or self.phase_time >= 7.0:
                self._face_target()
                self.dragon.velocity.update(0, 0)
                self.dragon.target = self.dragon.pos.copy()
                self.dragon.start_flying_attack()
                self.phase = self.AIR_ATTACK
                self.phase_time = 0.0
                self.impact_applied = False
                self.last_decision = f"ATTACKING FROM AIR: {target.label}"
            return

        if self.phase == self.AIR_ATTACK:
            if target is None:
                self.phase = self.SELECT
                return
            if self.dragon.state == self.dragon.FLY_ATTACK:
                # One gameplay hit per attack animation, not one hit per particle/frame.
                if self.dragon.state_time >= 0.82 and not self.impact_applied:
                    self._apply_target_hit()
            elif self.phase_time >= 0.25:
                self.phase = self.COOLDOWN
                self.phase_time = 0.0
                self.cooldown = 0.42
            return

        if self.phase == self.APPROACH_GROUND:
            if target is None:
                self.phase = self.SELECT
                return
            distance = self.dragon.pos.distance_to(self.approach_point)
            if (distance <= 58.0 and self.dragon.velocity.length() <= 115.0) or self.phase_time >= 7.0:
                self.dragon.start_landing(self.approach_point)
                self.phase = self.LANDING
                self.phase_time = 0.0
                self.last_decision = f"LANDING NEAR: {target.label}"
            return

        if self.phase == self.LANDING:
            if target is None:
                self.phase = self.SELECT
                return
            if self.dragon.state == self.dragon.GROUNDED:
                self._face_target()
                self.dragon.start_ground_attack()
                self.phase = self.GROUND_ATTACK
                self.phase_time = 0.0
                self.impact_applied = False
                self.last_decision = f"GROUND ATTACK: {target.label}"
            elif self.phase_time >= 4.2:
                # A defensive fallback in case a movement/landing edge case stalls.
                self.dragon.pos = self.approach_point.copy()
                self.dragon.velocity.update(0, 0)
                self.dragon._finish_landing()
            return

        if self.phase == self.GROUND_ATTACK:
            if target is None:
                self.phase = self.SELECT
                return
            if self.dragon.state == self.dragon.GROUND_ATTACK:
                if self.dragon.state_time >= 1.08 and not self.impact_applied:
                    self._apply_target_hit()
            elif self.phase_time >= 0.25:
                self.phase = self.COOLDOWN
                self.phase_time = 0.0
                self.cooldown = 0.42
            return

        if self.phase == self.COMPLETE:
            self.dragon.autopilot = True

    def draw_debug(self, surface: pygame.Surface, font: pygame.font.Font) -> None:
        if not self.enabled:
            return
        if self.current_target is not None and not self.current_target.destroyed:
            start = (round(self.dragon.pos.x), round(self.dragon.pos.y))
            end = self.current_target.rect.center
            pygame.draw.line(surface, GB_MID_DARK, start, end, 1)
            pygame.draw.circle(surface, GB_BRIGHT, end, 4, 1)





# Runtime adapter configuration. The host GameScene supplies live field geometry.
def configure_runtime(field_rect: pygame.Rect, asset_dir: Path | None = None) -> None:
    global FIELD, ASSET_DIR
    if field_rect.width < 160 or field_rect.height < 120:
        raise ValueError("Adventure Road field is too small")
    FIELD = pygame.Rect(field_rect)
    if asset_dir is not None:
        ASSET_DIR = Path(asset_dir)

