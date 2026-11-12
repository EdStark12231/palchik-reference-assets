"""EVT_004 Adventure Road presentation adapter for the live GameScene renderer.

Draws the dragon and damaged interface targets over the freshly rendered
GameScene surface. The host game owns the surface and input; this module only
adds the event overlay layer.
"""
from __future__ import annotations

import pygame
from typing import Optional

from .adventure_road_runtime import AdventureRoadRuntime, TargetSpec
from .adventure_road_dragon import InterfaceTarget


class AdventureRoadPresentation:
    """Thin overlay: reads live GameScene UI rects and draws the event on top."""

    # Target keys mapped to GameScene attribute names and default sizes.
    TARGET_SPECS = (
        TargetSpec(key="back", label="КНОПКА НАЗАД", kind="button", priority=1,
                   attack_mode="air", hp=2),
        TargetSpec(key="target", label="ЦЕЛЕВАЯ КНОПКА", kind="button", priority=2,
                   attack_mode="air", hp=2),
        TargetSpec(key="phrase", label="КНОПКА ФРАЗЫ", kind="button", priority=1,
                   attack_mode="ground", hp=2),
        TargetSpec(key="instant_wash", label="МГНОВЕННАЯ МОЙКА", kind="button", priority=2,
                   attack_mode="air", hp=2),
        TargetSpec(key="free_wash", label="БЕСПЛАТНАЯ МОЙКА", kind="button", priority=1,
                   attack_mode="ground", hp=2),
        TargetSpec(key="wallpaper", label="ОБОИ", kind="button", priority=1,
                   attack_mode="air", hp=1),
    )

    def __init__(self, game_view):
        self.game_view = game_view
        self.font: Optional[pygame.font.Font] = None
        self._target_specs: list[TargetSpec] = list(self.TARGET_SPECS)
        self._field_rect: Optional[pygame.Rect] = None
        self._install_live_button_capture()

    def _install_live_button_capture(self) -> None:
        """Capture the actual primary button rect returned by GameScene."""
        view = self.game_view
        if getattr(view, "_evt004_capture_installed", False):
            return

        original_draw = getattr(view, "_draw_primary_target_button", None)
        if not callable(original_draw):
            return

        def captured_draw(*args, **kwargs):
            result = original_draw(*args, **kwargs)
            if isinstance(result, pygame.Rect):
                view.evt004_rect_target = result.copy()
            return result

        view._draw_primary_target_button = captured_draw
        view._evt004_capture_installed = True

    def _font(self) -> pygame.font.Font:
        if self.font is None:
            self.font = pygame.font.SysFont("Arial", 16, bold=True)
        return self.font

    def build_field_rect(self, surface_size: tuple[int, int]) -> pygame.Rect:
        """Play field for the dragon: most of the screen minus UI margins."""
        width, height = surface_size
        margin_x = int(width * 0.05)
        margin_y = int(height * 0.12)
        return pygame.Rect(
            margin_x, margin_y,
            width - 2 * margin_x,
            height - margin_y - int(height * 0.10),
        )

    def collect_target_rects(self, surface_size: tuple[int, int]) -> list[TargetSpec]:
        """Collect only exact UI rectangles captured during the latest GameScene draw."""
        view = self.game_view
        bounds = pygame.Rect(0, 0, *surface_size)

        # Labels, priorities and combat parameters remain defined centrally.
        templates = {spec.key: spec for spec in self.TARGET_SPECS}

        candidates = (
            ("back", "evt004_rect_back"),
            ("target", "evt004_rect_target"),
            ("phrase", "evt004_rect_phrase"),
            ("instant_wash", "evt004_rect_instant_wash"),
            ("free_wash", "evt004_rect_free_wash"),
            ("wallpaper", "evt004_rect_wallpaper"),
        )

        specs: list[TargetSpec] = []
        for key, attr in candidates:
            template = templates[key]
            raw = getattr(view, attr, None)
            if raw is None:
                continue

            try:
                rect = pygame.Rect(raw).clip(bounds)
            except (TypeError, ValueError):
                continue

            if rect.width <= 0 or rect.height <= 0:
                continue

            specs.append(TargetSpec(
                key=template.key,
                label=template.label,
                rect=rect,
                kind=template.kind,
                priority=template.priority,
                attack_mode=template.attack_mode,
                hp=template.hp,
            ))

        return specs

    def sync_live_targets(self, runtime: AdventureRoadRuntime,
                          surface_size: tuple[int, int]) -> None:
        """Update target geometry without resetting damage or HP."""
        runtime.sync_live_target_specs(
            self.collect_target_rects(surface_size)
        )

    def build_runtime(self, surface_size: tuple[int, int], project_root,
                      dragon_hp: int = 8, seed: int | None = None) -> AdventureRoadRuntime:
        """Create a fresh AdventureRoadRuntime with live UI targets."""
        field = self.build_field_rect(surface_size)
        self._field_rect = field
        specs = self.collect_target_rects(surface_size)
        return AdventureRoadRuntime(
            field_rect=field,
            target_specs=specs,
            project_root=project_root,
            dragon_hp=dragon_hp,
            seed=seed,
        )

    def draw(self, surface: pygame.Surface, runtime: AdventureRoadRuntime,
             background_snapshot: pygame.Surface) -> None:
        """Draw the dragon and target damage over the game surface."""
        if runtime is None or not runtime.running:
            return

        self.sync_live_targets(runtime, surface.get_size())
        runtime.set_cursor_position(pygame.mouse.get_pos())
        runtime.draw(surface, background_snapshot)
