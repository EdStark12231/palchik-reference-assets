"""EVT_004 Adventure Road runtime core; host integration is intentionally thin.

The host supplies the real GameScene field rectangle and target rectangles. This
module owns dragon combat, autonomous interface attacks, outcome and timing.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import pygame

from . import adventure_road_dragon as dragon_core

EVENT_ID = "EVT_004_ADVENTURE_ROAD"
COLLECTION_ID = "road_of_adventures"
TRIGGER_CODE = "111111"


@dataclass(frozen=True)
class TargetSpec:
    key: str
    label: str
    rect: pygame.Rect = field(default_factory=lambda: pygame.Rect(0, 0, 0, 0))
    kind: str = "button"
    priority: int = 1
    attack_mode: str = "air"
    hp: int = 2


@dataclass(frozen=True)
class EventOutcome:
    result: str  # VICTORY | DEFEAT | CANCELLED
    reason: str
    dragon_hp: int
    dragon_max_hp: int
    destroyed_targets: int
    total_targets: int


class AdventureRoadRuntime:
    """Frame-driven event logic. No SaveManager or GameLogic mutation occurs here."""

    VICTORY = "VICTORY"
    DEFEAT = "DEFEAT"
    RUNNING = "RUNNING"
    CANCELLED = "CANCELLED"

    def __init__(self, field_rect: pygame.Rect, target_specs: Iterable[TargetSpec], *,
                 project_root: Path, dragon_hp: int = 8, seed: int | None = None):
        if dragon_hp < 1:
            raise ValueError("dragon_hp must be positive")
        self.project_root = Path(project_root).resolve()
        self.field_rect = pygame.Rect(field_rect)
        self.asset_dir = self.project_root / "assets" / "games_sprites" / "adventure_road"
        dragon_core.configure_runtime(self.field_rect, self.asset_dir)
        self.assets = dragon_core.DragonAssets()
        self.dragon = dragon_core.Dragon(self.assets)
        self.targets = [
            dragon_core.InterfaceTarget(
                key=s.key, label=s.label, rect=pygame.Rect(s.rect), kind=s.kind,
                priority=s.priority, attack_mode=s.attack_mode,
                max_hp=max(1, int(s.hp)), hp=max(1, int(s.hp)),
            ) for s in target_specs
        ]
        if not self.targets:
            raise ValueError("Adventure Road requires at least one live interface target")
        self.ai = dragon_core.DragonAI(self.dragon, self.targets)
        self.ai.rng = random.Random(seed)
        self.dragon_hp_max = int(dragon_hp)
        self.dragon_hp = int(dragon_hp)
        self.player_attack_cooldown = 0.0
        self.player_attack_interval = 0.34
        self.player_invulnerability = 0.0
        self.player_hit_flash = 0.0
        self.elapsed = 0.0
        self.outcome: str | None = None
        self.outcome_reason = ""
        self._started = False
        self._finished = False
        self._audio_events_pending: list[str] = []
        self._last_audio_dragon_state = None
        self._wing_flap_audio_active = False
        self._destroyed_targets_audio_seen: set[str] = set()

    @property
    def running(self) -> bool:
        return self._started and not self._finished

    @property
    def remaining_targets(self) -> int:
        return sum(1 for t in self.targets if t.visible and not t.destroyed and t.hp > 0)

    @property
    def destroyed_targets(self) -> int:
        return sum(1 for t in self.targets if t.destroyed)

    @property
    def total_targets(self) -> int:
        return len(self.targets)

    def sync_live_target_specs(self, specs: Iterable[TargetSpec]) -> None:
        """Synchronize geometry while preserving target combat state."""
        fresh = {spec.key: spec for spec in specs}

        for target in self.targets:
            spec = fresh.pop(target.key, None)

            if spec is None:
                target.visible = False
                continue

            target.rect.update(spec.rect)
            target.visible = True

        for spec in fresh.values():
            target = dragon_core.InterfaceTarget(
                key=spec.key,
                label=spec.label,
                rect=pygame.Rect(spec.rect),
                kind=spec.kind,
                priority=spec.priority,
                attack_mode=spec.attack_mode,
                max_hp=max(1, int(spec.hp)),
                hp=max(1, int(spec.hp)),
            )
            self.targets.append(target)

        self.ai.targets = self.targets

    def set_cursor_position(self, position) -> None:
        """Cursor is a transient pursuit point, not a destructible target."""
        if position is None:
            self.ai.set_cursor_position(None)
            return

        point = pygame.Vector2(position)

        if self.field_rect.collidepoint(round(point.x), round(point.y)):
            self.ai.set_cursor_position(point)
        else:
            self.ai.set_cursor_position(None)

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.dragon.start_appearance(pygame.Vector2(self.field_rect.center))
        self.ai.enable()
        self._capture_audio_transitions()

    def update(self, dt: float) -> EventOutcome | None:
        if not self.running:
            return self.result()
        dt = max(0.0, min(float(dt), 0.05))
        self.elapsed += dt
        self.player_attack_cooldown = max(0.0, self.player_attack_cooldown - dt)
        self.player_invulnerability = max(0.0, self.player_invulnerability - dt)
        self.player_hit_flash = max(0.0, self.player_hit_flash - dt)
        self.dragon.update(dt)
        self.ai.update(dt)
        for target in self.targets:
            target.update(dt)
        self._capture_audio_transitions()
        if self.dragon.state == self.dragon.DEATH and self.dragon.death_elapsed >= 1.7:
            self._finish(self.VICTORY, "DRAGON_DEFEATED")
        elif self.remaining_targets == 0:
            self._finish(self.DEFEAT, "ALL_INTERFACE_TARGETS_DESTROYED")
        return self.result()

    def player_attack(self, world_position: tuple[int, int] | pygame.Vector2) -> bool:
        """Register one sword swing at a world-space point; call on left click."""
        if not self.running or self.dragon.state == self.dragon.DEATH:
            return False
        point = pygame.Vector2(world_position)
        # The click must intersect the dragon's combat radius, not merely the screen.
        hit_radius = 68.0
        if point.distance_to(self.dragon.pos) > hit_radius:
            return False
        # A confirmed click on the dragon provokes cursor aggression.
        self.ai.on_dragon_clicked()
        if self.player_attack_cooldown > 0:
            return False
        self.player_attack_cooldown = self.player_attack_interval
        if self.player_invulnerability > 0:
            return True
        self.dragon_hp = max(0, self.dragon_hp - 1)
        self.player_invulnerability = 0.42
        self.player_hit_flash = 0.22
        self.dragon.start_hit()
        if self.dragon_hp == 0:
            self.ai.disable()
            self.dragon.start_death()
        return True

    def cancel(self, reason: str = "HOST_CANCELLED") -> EventOutcome:
        if not self._finished:
            self._finish(self.CANCELLED, reason)
        return self.result()  # type: ignore[return-value]

    def _finish(self, outcome: str, reason: str) -> None:
        if self._finished:
            return
        self.outcome = outcome
        self.outcome_reason = reason
        self._finished = True
        self.ai.disable()
        if self._wing_flap_audio_active:
            self._audio_events_pending.append("wing_flap_loop_stop")
            self._wing_flap_audio_active = False

    def result(self) -> EventOutcome | None:
        if self.outcome is None:
            return None
        return EventOutcome(
            result=self.outcome, reason=self.outcome_reason,
            dragon_hp=self.dragon_hp, dragon_max_hp=self.dragon_hp_max,
            destroyed_targets=self.destroyed_targets, total_targets=len(self.targets),
        )

    def draw(self, surface: pygame.Surface, background_snapshot: pygame.Surface) -> None:
        """Draw overlays over the host's freshly rendered GameScene surface."""
        for target in self.targets:
            target.draw_damage(surface, background_snapshot)
        self.dragon.draw(surface)

    def _capture_audio_transitions(self) -> None:
        """Queue one-shot cues from dragon state changes and destroyed UI targets."""
        state = getattr(self.dragon, "state", None)
        if state != self._last_audio_dragon_state:
            state_cues = (
                ("APPEAR", "appearance"),
                ("ROAR", "roar"),
                ("FLY_ATTACK", "fire_breath"),
                ("GROUND_ATTACK", "ground_attack"),
                ("LANDING", "landing"),
                ("HIT", "hit"),
                ("DEATH", "death"),
            )
            for state_name, sound_key in state_cues:
                state_value = getattr(self.dragon, state_name, state_name)
                if state == state_value:
                    self._audio_events_pending.append(sound_key)
                    break

            flight_values = {
                getattr(self.dragon, "FLIGHT", "FLIGHT"),
                getattr(self.dragon, "FLY_ATTACK", "FLY_ATTACK"),
            }
            should_loop = state in flight_values
            if should_loop and not self._wing_flap_audio_active:
                self._audio_events_pending.append("wing_flap_loop_start")
                self._wing_flap_audio_active = True
            elif not should_loop and self._wing_flap_audio_active:
                self._audio_events_pending.append("wing_flap_loop_stop")
                self._wing_flap_audio_active = False
            self._last_audio_dragon_state = state

        for target in self.targets:
            key = str(getattr(target, "key", ""))
            if getattr(target, "destroyed", False) and key not in self._destroyed_targets_audio_seen:
                self._destroyed_targets_audio_seen.add(key)
                self._audio_events_pending.append("interface_break")

    def drain_audio_events(self) -> tuple[str, ...]:
        """Return and clear audio cues accumulated since the previous frame."""
        events = tuple(self._audio_events_pending)
        self._audio_events_pending.clear()
        return events

    def audio_events(self) -> tuple[str, ...]:
        """Return stable event keys for host audio routing; no mixer ownership here."""
        return (
            "appearance", "roar", "wing_flap_loop", "fire_breath", "ground_attack",
            "landing", "hit", "death", "interface_break",
        )
