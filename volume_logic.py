# volumeloggin_utils.logic.py
# Utilities to measure volume/mass and iteratively balance B1 to match B2 by
# nudging the B1 pDeformDiamADJ user parameter (document-only; no library lookups).

import adsk.core, adsk.fusion, math, traceback, re
from datetime import datetime
from . import loggin_utils
CC_PER_IN3 = 16.387064

# ----- small helpers -----
def _do_events_and_refresh():
    try:
        adsk.doEvents()
        app = adsk.core.Application.get()
        if app:
            app.activeViewport.refresh()
    except:
        pass

def _relative_diff(a: float, b: float) -> float:
    """|a-b| / max(|b|, tiny); returns fraction (0.001 == 0.1%)."""
    tiny = 1e-12
    return abs(a - b) / max(abs(b), tiny)

def _find_user_param(design: adsk.fusion.Design, candidates):
    """Try exact names first, then fuzzy match for 'deform'+'adj' with 'b1' bias."""
    try:
        up = design.userParameters
        # exact
        for nm in candidates:
            p = up.itemByName(nm)
            if p:
                return p
        # fuzzy
        for i in range(up.count):
            p = up.item(i)
            name_l = (p.name or "").lower()
            if ("deform" in name_l and "adj" in name_l) and ("b1" in name_l or name_l.startswith("pdeform")):
                return p
    except:
        pass
    return None

def _nudge_param_relative(param: adsk.fusion.UserParameter, rel_step: float) -> float:
    """
    Multiply current param.value by (1 + rel_step) and set it.
    Returns the new absolute value.
    """
    cur = float(param.value)
    factor = 1.0 + rel_step
    new_val = cur * factor
    if new_val <= 0:
        # keep positive and sane if a user had a very small value
        new_val = max(abs(cur) * 0.5, 1e-6)
    param.value = new_val
    return new_val

# ----- public API -----
def measure_current_design() -> dict:
    """
    Sum volume & mass for all solid bodies in the active design's full tree.
    Units are whatever Fusion reports in PhysicalProperties (unitless for our math).
    Returns: {"ok": bool, "volume": float, "mass": float}
    """
    try:
        app = adsk.core.Application.get()
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            return {"ok": False, "reason": "No active design."}

        total_vol = 0.0
        total_mass = 0.0

        def walk(comp: adsk.fusion.Component):
            nonlocal total_vol, total_mass
            # bodies on this component
            bodies = getattr(comp, "bRepBodies", None)
            if bodies:
                for i in range(bodies.count):
                    b = bodies.item(i)
                    if getattr(b, "isSolid", False):
                        try:
                            props = getattr(b, "physicalProperties", None)
                            if props is None and hasattr(b, "getPhysicalProperties"):
                                props = b.getPhysicalProperties()
                            if props:
                                total_vol += float(props.volume)
                                total_mass += float(props.mass)
                        except:
                            pass
            # recurse
            occs = getattr(comp, "occurrences", None)
            if occs:
                for i in range(occs.count):
                    try:
                        walk(occs.item(i).component)
                    except:
                        pass

        walk(design.rootComponent)
        return {"ok": True, "volume": total_vol, "mass": total_mass}
    except Exception as e:
        loggin_utils.log(f"[volumeloggin_utils.logic] measure error: {e}\n{traceback.format_exc()}")
        return {"ok": False, "reason": str(e)}

def _measured_in3() -> tuple[float, float]:
    """Return (volume_in3, mass_g) from measure_current_design() regardless of keys."""
    m = measure_current_design()
    if not m.get("ok"):
        raise RuntimeError(m.get("reason", "measure failed"))
    if "volume_in3" in m:
        return float(m["volume_in3"]), float(m.get("mass_g", 0.0))
    # back-compat: 'volume' in cm^3, 'mass' in g
    v_in3 = float(m["volume"]) / CC_PER_IN3
    return v_in3, float(m.get("mass", 0.0))

def _param_ref_scale(param, floor=1e-3):
    """Use a stable reference for % steps so we don't scale off ~0."""
    try:
        v = abs(float(param.value))
    except Exception:
        v = 0.0
    return max(v, floor)  # floor in *param units* (unitless in your ADJ case)

def _param_ref_scale(param, floor=1e-3):
    v = abs(float(param.value)) if hasattr(param, "value") else 0.0
    return max(v, floor)

def _nudge_param_percent_of_ref(param, pct, ref):
    cur = float(param.value)
    param.value = cur + (pct/100.0)*ref


def _nudge_param_percent_of_ref(param, pct, ref):
    """Apply a +/-pct% step, but as an absolute delta using ref as the base."""
    cur = float(param.value)
    delta = (pct / 100.0) * ref
    param.value = cur + delta  # absolute add, not multiplicative
'''
def balance_b1_to_volume(
    target_b2_volume: float,             # <-- pass IN^3 here
    *,
    primary_tol: float = 0.01,           # 1%
    final_tol: float = 0.001,            # 0.1%
    primary_step: float = .01,          # 1%
    fine_step: float = 0.001,            # 0.1%
    max_iters: int = 200,
    param_candidates = (
        "B1 pDeformDiamADJ","pDeformDiamADJ","b1_pDeformDiamADJ",
        "b1DeformDiamADJ","b1DeformDiamAdj","b1_deform_diam_adj","pDeformDiamADJ_B1", "pHeadHeightADJ"
    ),
    autodetect_direction: bool = True,
    clamp_min: float | None = None,
    clamp_max: float | None = None,
) -> dict:
    """
    Balance B1 so its volume (IN^3) matches target_b2_volume (IN^3).
    Returns keys in IN^3: b1_volume_in3, b2_volume_in3; also b1_volume_cc for convenience.
    """
    try:
        app = adsk.core.Application.get()
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            return {"ok": False, "reason": "No active design."}
        if target_b2_volume <= 0:
            return {"ok": False, "reason": "Target B2 volume must be positive."}

        # Find parameter
        param = _find_user_param(design, param_candidates)
        if not param:
            return {"ok": False, "reason": f"Parameter not found (tried {list(param_candidates)})"}

        # Initial measure in IN^3
        b1_in3, b1_mass_g = _measured_in3()
        rel = _relative_diff(b1_in3, target_b2_volume)
        loggin_utils.log(f"[balance] start: B1 = {b1_in3:.6f} in^3 ({b1_in3*CC_PER_IN3:.6f} cm^3); "
             f"B2 target = {target_b2_volume:.6f} in^3; rel={rel*100:.4f}%")

        if rel <= final_tol:
            return {
                "ok": True, "iterations": 0,
                "b1_volume_in3": b1_in3, "b1_volume_cc": b1_in3*CC_PER_IN3,
                "b1_mass_g": b1_mass_g,
                "b2_volume_in3": target_b2_volume,
                "final_param_value": float(param.value),
                "within": rel
            }

        # Autodetect direction (in IN^3)
        sign = None
        if autodetect_direction:
            base_val = float(param.value)
            try:
                _nudge_param_relative(param, +0.005)  # +0.5%
                _do_events_and_refresh()
                v_probe_in3, _ = _measured_in3()
                param.value = base_val
                _do_events_and_refresh()
                sign = +1 if v_probe_in3 > b1_in3 else -1
                loggin_utils.log(f"[balance] autodetect: Δparam +0.5% -> Δvol {v_probe_in3 - b1_in3:+.6f} in^3 => sign={sign}")
                # refresh baseline after revert
                b1_in3, b1_mass_g = _measured_in3()
                rel = _relative_diff(b1_in3, target_b2_volume)
            except Exception as e:
                loggin_utils.log(f"[balance] autodetect failed: {e}")

        it = 0
        last_rel = rel
        while it < max_iters:
            it += 1
            step = primary_step if rel > primary_tol else fine_step

            # Direction: if sign known, use it; else assume bigger param => bigger volume
            want_up = (b1_in3 < target_b2_volume)
            go_up = (want_up if sign is None else (want_up if sign > 0 else not want_up))
            # Use a reference scale so % means something even if value ~0
            ref = _param_ref_scale(param, floor=1e-3)

            # --- probe (measure local slope using +0.5% of ref) ---
            PROBE_PCT = 0.5
            base_val = float(param.value)
            try:
                _nudge_param_percent_of_ref(param, +PROBE_PCT, ref)
                _do_events_and_refresh()
                v_plus_in3, _ = _measured_in3()
            finally:
                param.value = base_val
                _do_events_and_refresh()

            dV = v_plus_in3 - b1_in3
            if abs(dV) < 1e-9:
                # If 0.5% is too small to see, re-probe at 2% (of ref)
                PROBE_PCT = 2.0
                _nudge_param_percent_of_ref(param, +PROBE_PCT, ref)
                _do_events_and_refresh()
                v_plus_in3, _ = _measured_in3()
                param.value = base_val
                _do_events_and_refresh()
                dV = v_plus_in3 - b1_in3

            slope_in3_per_pct = (dV / PROBE_PCT) if PROBE_PCT else 0.0   # in^3 per 1% (of ref)
            ERR = b1_in3 - target_b2_volume

            # --- decide step in % (of ref), then apply as absolute delta ---
            GAIN     = 0.7
            MAX_STEP = 5.0    # clamp (% of ref)
            MIN_STEP = 0.2
            CLOSE    = 0.2    # 0.2% relative error → tiny trims

            if abs(slope_in3_per_pct) > 1e-12:
                newton_pct = -ERR / slope_in3_per_pct
                step_pct   = max(-MAX_STEP, min(MAX_STEP, newton_pct * GAIN))
            else:
                # fallback to your coarse/fine, **but convert fraction->percent**
                frac = (primary_step if rel > primary_tol else fine_step)  # e.g. 0.01 or 0.001
                want_up = (b1_in3 < target_b2_volume)
                go_up   = (want_up if sign is None else (want_up if sign > 0 else not want_up))
                step_pct = (frac * 100.0) * (+1 if go_up else -1)

            # feather near target
            rel_err_pct = 100.0 * abs(ERR / target_b2_volume)
            if rel_err_pct < CLOSE:
                step_pct = max(-MIN_STEP, min(MIN_STEP, step_pct))

            # apply as absolute delta based on ref
            _nudge_param_percent_of_ref(param, step_pct, ref)
            loggin_utils.log(f"[balance] iter {it}: {param.name} step {step_pct:+.3f}% (of ref={ref:g}) -> {float(param.value):.6g}")
            _do_events_and_refresh()


            # Re-measure in IN^3
            b1_in3, b1_mass_g = _measured_in3()
            rel = _relative_diff(b1_in3, target_b2_volume)
            loggin_utils.log(f"[balance] iter {it}: B1 = {b1_in3:.6f} in^3 ({b1_in3*CC_PER_IN3:.6f} cm^3), "
                 f"rel={rel*100:.4f}%")

            if rel <= final_tol:
                loggin_utils.log(f"[balance] ✅ converged within {final_tol*100:.1f}% in {it} iters.")
                return {
                    "ok": True, "iterations": it,
                    "b1_volume_in3": b1_in3, "b1_volume_cc": b1_in3*CC_PER_IN3,
                    "b1_mass_g": b1_mass_g,
                    "b2_volume_in3": target_b2_volume,
                    "final_param_value": float(param.value),
                    "within": rel
                }

            if rel > last_rel:
                loggin_utils.log("[balance] ⚠️ error increased; reducing step / flipping direction next")
                sign = (-sign) if sign is not None else None
                primary_step = fine_step
            last_rel = rel

        loggin_utils.log(f"[balance] ❌ max_iters {max_iters} reached; final rel={rel*100:.4f}%")
        return {
            "ok": False, "reason": f"Max iterations {max_iters} reached.",
            "iterations": it,
            "b1_volume_in3": b1_in3, "b1_volume_cc": b1_in3*CC_PER_IN3,
            "b1_mass_g": b1_mass_g,
            "b2_volume_in3": target_b2_volume,
            "final_param_value": float(param.value),
            "within": rel
        }

    except Exception as e:
        loggin_utils.log(f"[balance] exception: {e}\n{traceback.format_exc()}")
        return {"ok": False, "reason": str(e)}
'''

def balance_b1_to_volume(
    target_b2_volume: float,             # IN^3
    *,
    # — legacy knobs kept for API compatibility (ignored by ladder) —
    primary_tol: float = 0.01,           # (unused) was 1%
    final_tol: float = 0.001,            # 0.1% relative error stop
    primary_step: float = 0.01,          # (unused) was 1%
    fine_step: float = 0.001,            # (unused) was 0.1%
    max_iters: int = 500,
    param_candidates = (
        "B1 pDeformDiamADJ","pDeformDiamADJ","b1_pDeformDiamADJ",
        "b1DeformDiamADJ","b1DeformDiamAdj","b1_deform_diam_adj",
        "pDeformDiamADJ_B1","pHeadHeightADJ"
    ),
    autodetect_direction: bool = True,
    clamp_min: float | None = None,
    clamp_max: float | None = None,
    # — ladder controls —
    rungs: tuple[float, ...] = (10.0, 1.0, 0.1, 0.01, 0.001),  # % of ref
    revert_on_crossover: bool = True,      # undo the overshoot before dropping rung
) -> dict:
    """
    Replace the prior Newton/coarse-fine tuning with a LADDER method:
      • Move the control param in fixed % steps of a reference magnitude.
      • When the error crosses the target (sign flip), optionally revert that step,
        flip direction, and drop to the next smaller rung (10% → 1% → 0.1% → 0.01% → 0.001%).
      • Stop when relative error ≤ final_tol, or rungs/iters are exhausted.

    Notes:
      - `primary_tol`/`primary_step`/`fine_step` are retained for compatibility but not used.
      - Uses your helpers: _find_user_param, _measured_in3, _param_ref_scale,
        _nudge_param_percent_of_ref, _nudge_param_relative, _do_events_and_refresh,
        CC_PER_IN3, and loggin_utils.log.
    """
    try:
        app = adsk.core.Application.get()
        design = adsk.fusion.Design.cast(app.activeProduct)
        if not design:
            return {"ok": False, "reason": "No active design."}
        if target_b2_volume <= 0:
            return {"ok": False, "reason": "Target B2 volume must be positive."}

        # 1) Find control parameter
        param = _find_user_param(design, param_candidates)
        if not param:
            return {"ok": False, "reason": f"Parameter not found (tried {list(param_candidates)})"}

        # 2) Initial measure
        b1_in3, b1_mass_g = _measured_in3()
        rel = abs((b1_in3 - target_b2_volume) / target_b2_volume)
        loggin_utils.log(
            f"[ladder] start: B1={b1_in3:.6f} in^3 ({b1_in3*CC_PER_IN3:.6f} cm^3); "
            f"B2 target={target_b2_volume:.6f} in^3; rel={rel*100:.4f}%"
        )
        if rel <= final_tol:
            return {
                "ok": True, "iterations": 0,
                "b1_volume_in3": b1_in3, "b1_volume_cc": b1_in3*CC_PER_IN3,
                "b1_mass_g": b1_mass_g,
                "b2_volume_in3": target_b2_volume,
                "final_param_value": float(param.value),
                "within": rel
            }

        # 3) Direction hint: does +param raise or lower volume?
        sign_hint = None  # +1 means +param => +volume; -1 means +param => -volume
        if autodetect_direction:
            base_val = float(param.value)
            try:
                _nudge_param_relative(param, +0.005)  # +0.5%
                _do_events_and_refresh()
                v_probe_in3, _ = _measured_in3()
                param.value = base_val
                _do_events_and_refresh()
                sign_hint = +1 if (v_probe_in3 - b1_in3) > 0 else -1
                loggin_utils.log(
                    f"[ladder] autodetect: Δparam +0.5% -> Δvol {v_probe_in3 - b1_in3:+.6f} => sign_hint={sign_hint}"
                )
                # refresh baseline after revert
                b1_in3, b1_mass_g = _measured_in3()
            except Exception as e:
                loggin_utils.log(f"[ladder] autodetect failed: {e}")

        # 4) Reference scale for percent steps (keeps % meaningful near zero)
        ref = _param_ref_scale(param, floor=1e-3)

        def want_increase_volume(curr: float, target: float) -> bool:
            return curr < target

        def current_direction_for_param(curr_b1: float) -> int:
            inc_vol = want_increase_volume(curr_b1, target_b2_volume)
            # If sign_hint is None, assume +param => +volume
            if sign_hint is None:
                return +1 if inc_vol else -1
            # Translate desired volume change into parameter direction via sign_hint
            return (+1 if inc_vol else -1) * sign_hint

        def apply_step_pct(pct: float):
            """Apply step as % of ref; clamp; refresh; return (prev_val, new_val)."""
            prev_val = float(param.value)
            _nudge_param_percent_of_ref(param, pct, ref)
            if clamp_min is not None or clamp_max is not None:
                v = float(param.value)
                if clamp_min is not None and v < clamp_min:
                    v = clamp_min
                if clamp_max is not None and v > clamp_max:
                    v = clamp_max
                if v != float(param.value):
                    param.value = v
            _do_events_and_refresh()
            return prev_val, float(param.value)

        it = 0
        err = b1_in3 - target_b2_volume
        prev_err_sign = 1 if err > 0 else -1
        direction = current_direction_for_param(b1_in3)

        # 5) Ladder loop
        # --- at top of ladder loop, BEFORE the while for each rung ---
        for rung_idx, rung_pct in enumerate(rungs, start=1):
            step_mag = abs(rung_pct)  # percent of ref
            # ⬇️ NEW: always recompute direction at the start of each rung
            direction = current_direction_for_param(b1_in3)
            loggin_utils.log(f"[ladder] rung {rung_idx}/{len(rungs)}: step={step_mag:.4g}% of ref={ref:g} dir={direction:+d}")

            # (optional) cap steps per rung to avoid runaway on one rung
            rung_steps = 0
            RUNG_STEP_CAP = 40  # tweak as you like
            while it < max_iters and rung_steps < RUNG_STEP_CAP:
                rung_steps += 1
                it += 1
                step_pct = step_mag * direction
                prev_param_val = float(param.value)
                prev_b1 = b1_in3
                prev_err = b1_in3 - target_b2_volume
                prev_abs = abs(prev_err)

                # take a step
                _old_val, _new_val = apply_step_pct(step_pct)
                loggin_utils.log(f"[ladder] iter {it}: {param.name} {step_pct:+.4f}% -> {float(param.value):.6g}")

                # measure
                b1_in3, b1_mass_g = _measured_in3()
                err = b1_in3 - target_b2_volume
                rel = abs(err / target_b2_volume)
                curr_err_sign = 1 if err > 0 else -1
                loggin_utils.log(f"[ladder] iter {it}: B1={b1_in3:.6f} in^3 ({b1_in3*CC_PER_IN3:.6f} cm^3), rel={rel*100:.4f}%")

                # stop if good enough
                if rel <= final_tol:
                    loggin_utils.log(f"[ladder] ✅ converged ≤ {final_tol*100:.3f}% in {it} iters.")
                    return {
                        "ok": True, "iterations": it,
                        "b1_volume_in3": b1_in3, "b1_volume_cc": b1_in3*CC_PER_IN3,
                        "b1_mass_g": b1_mass_g,
                        "b2_volume_in3": target_b2_volume,
                        "final_param_value": float(param.value),
                        "within": rel
                    }

                # ↔ cross-over? (sign flip)
                if curr_err_sign != prev_err_sign:
                    loggin_utils.log(f"[ladder] ↔ cross-over at rung {rung_idx}.")
                    if revert_on_crossover:
                        # revert that last move to sit just before the boundary
                        param.value = prev_param_val
                        _do_events_and_refresh()
                        b1_in3, b1_mass_g = _measured_in3()
                        err = b1_in3 - target_b2_volume
                        rel = abs(err / target_b2_volume)
                        loggin_utils.log(f"[ladder]   reverted step; B1={b1_in3:.6f} in^3; rel={rel*100:.4f}%")

                    # ⬇️ NEW: recompute direction against target after revert
                    direction = current_direction_for_param(b1_in3)
                    prev_err_sign = 1 if err > 0 else -1
                    break  # advance to next rung

                # ⬇️ NEW: worsen-guard — if we moved the wrong way (|err| increased), undo & flip
                if abs(err) > prev_abs:
                    loggin_utils.log("[ladder]   step worsened error; reverting and flipping direction.")
                    param.value = prev_param_val
                    _do_events_and_refresh()
                    b1_in3, b1_mass_g = _measured_in3()
                    # flip and continue stepping on this rung in the new direction
                    direction *= -1
                    # keep prev_err_sign as-is (no crossover), continue while-loop
                    continue

                # keep stepping on same rung
                prev_err_sign = curr_err_sign

        # If we get here, we ran out of rungs or iterations
        status_ok = (rel <= final_tol)
        reason = "Rungs exhausted" if it < max_iters else "Reached max iterations"
        loggin_utils.log(f"[ladder] ❌ {reason}; rel={rel*100:.4f}% after {it} iters.")
        return {
            "ok": status_ok,
            "reason": reason,
            "iterations": it,
            "b1_volume_in3": b1_in3, "b1_volume_cc": b1_in3*CC_PER_IN3,
            "b1_mass_g": b1_mass_g,
            "b2_volume_in3": target_b2_volume,
            "final_param_value": float(param.value),
            "within": rel
        }

    except Exception as e:
        loggin_utils.log(f"[ladder] exception: {e}\n{traceback.format_exc()}")
        return {"ok": False, "reason": str(e)}
