# Design System Specification: The Ephemeral Gallery

## 1. Overview & Creative North Star
This design system is built upon the Creative North Star of **"The Ephemeral Gallery."** We are not building a standard utility app; we are crafting a digital exhibition that feels as fluid as steamed milk and as grounded as a double-shot of espresso.

To move beyond the "template" look, we reject rigid, boxed-in layouts in favor of **Intentional Asymmetry**. Elements should feel like they are floating on a curated plane. We utilize overlapping containers, generous white space (the "milky foam"), and a high-contrast typographic scale to create an editorial rhythm that guides the eye through sophisticated focal points.

## 2. Colors: The Tonal Brew
The palette is a sophisticated interplay between the depth of the bean and the lightness of the froth.

### The "No-Line" Rule
Standard 1px solid borders are strictly prohibited for sectioning. Structural boundaries must be defined through:
1.  **Background Color Shifts:** Placing a `surface-container-low` (#f4f4f0) section against a `surface` (#faf9f5) background.
2.  **Tonal Transitions:** Using the spacing scale to let negative space act as the divider.

### Surface Hierarchy & Nesting
Treat the UI as a physical stack of semi-transparent materials. Use the surface tiers to create "nested" depth:
*   **Base Layer:** `surface` (#faf9f5).
*   **Secondary Content:** `surface-container-low` (#f4f4f0).
*   **Interactive Floating Elements:** `surface-container-lowest` (#ffffff) with a glass effect.

### The "Glass & Gradient" Rule
To achieve a premium, custom feel, floating elements (modals, navigation bars, hover cards) must utilize **Glassmorphism**. Combine a semi-transparent `surface` color with a `backdrop-blur` (recommended 12px–20px). 
*   **Signature Texture:** For Hero sections or primary CTAs, apply a subtle linear gradient from `primary` (#271310) to `primary_container` (#3e2723) at a 135-degree angle to provide "visual soul."

## 3. Typography: Editorial Authority
We pair the high-end serif **Newsreader** with the functional clarity of **Manrope**.

*   **Display & Headlines (Newsreader):** These are your "art pieces." Use `display-lg` (3.5rem) with tight letter-spacing to create an authoritative, editorial presence. Headlines should often be placed with intentional asymmetry (e.g., left-aligned with a significant right-side margin).
*   **Body & Labels (Manrope):** Use Manrope for all functional text. It provides a modern, clean counterpoint to the organic nature of Newsreader. 
*   **Hierarchy:** Always maintain a clear "Visual Anchor." If a `display-lg` header is used, the surrounding `body-md` text should have ample breathing room (using `spacing-8` or `spacing-10`) to prevent the layout from feeling cluttered.

## 4. Elevation & Depth
In this system, depth is a whisper, not a shout.

*   **The Layering Principle:** Achieve lift by stacking surface tiers. A `surface-container-lowest` card placed on a `surface-dim` background creates a natural, soft lift without the need for heavy shadows.
*   **Ambient Shadows:** For elements that must float (like a FAB or a floating Nav), use an extra-diffused shadow: `blur: 24px`, `spread: -4px`, and `color: opacity 6%` of the `primary` (#271310) token. This mimics natural ambient light hitting a matte surface.
*   **The "Ghost Border" Fallback:** If a container requires a boundary for accessibility, use a **Ghost Border**. Apply the `outline-variant` (#d3c3c0) at **15% opacity**. Never use a 100% opaque border.
*   **Glassmorphism Integration:** Apply `backdrop-blur` to any element using the `surface_container_lowest` token to allow background tones to bleed through, softening the interface's edges.

## 5. Components

### Buttons
*   **Primary:** A solid `primary` (#271310) fill with `on-primary` (#ffffff) text. Use `ROUND_EIGHT` (0.5rem) for a modern feel.
*   **Secondary (Glass):** A semi-transparent `surface-container-highest` with a `backdrop-blur`. This makes the button feel "carved" out of the glass.
*   **Tertiary:** Purely typographic using `label-md` in `primary` color, with a subtle underline that appears on hover.

### Cards & Lists
*   **The Barrier-Free Rule:** Forbid the use of divider lines between list items. Instead, use a background shift to `surface-container-low` on hover, or use `spacing-4` (1.4rem) to separate items visually.
*   **Editorial Cards:** Use `ROUND_EIGHT` and `surface-container-lowest`. Overlap the image slightly outside the card boundary for an artisanal, "broken-grid" effect.

### Input Fields
*   **Soft Focus:** Inputs should use `surface-container-low` with no border. On focus, transition to a "Ghost Border" (`outline-variant` at 20%) and a very subtle ambient shadow.

### Signature Component: The Fluid Navigation
Instead of a fixed top bar, use a floating, glassmorphic navigation pill at the bottom or top of the viewport. It should use `surface_container_lowest` with 80% opacity and a 16px backdrop-blur.

## 6. Do's and Don'ts

### Do:
*   **Embrace Negative Space:** Use `spacing-16` and `spacing-20` to separate major content blocks.
*   **Layer Surfaces:** Think in 3D—put the darkest coffee tones (`primary`) behind the lightest milky tones (`surface`) for high-impact sections.
*   **Use Subtle Animation:** Elements should fade and slide (200ms-300ms) to mimic the fluidity of liquid.

### Don't:
*   **Don't use 1px Dividers:** Reach for a color shift or white space first.
*   **Don't use Pure Black:** Always use `primary` (#271310) for deep tones to keep the palette organic.
*   **Don't Symmetrize Everything:** Rigid grids are for spreadsheets; Artista Amara is an experience. Allow elements to "breathe" off-center.
*   **Don't Over-round:** Stick strictly to `ROUND_FOUR` (0.25rem) for small items and `ROUND_EIGHT` (0.5rem) for containers. Avoid "pill" shapes unless specified for small tags/chips.