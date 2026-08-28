# Dashboard Responsive Layout Guidelines

This document defines the shared guidelines for designing and reviewing responsive layouts in the Codex Token Bola dashboard.

It applies to the browser UI in `scripts/assets/dashboard.html`, `scripts/assets/dashboard.css`, and `scripts/assets/dashboard/`.

## Core Principle

Do not scale every component by the same ratio when the screen size changes.

Combine the following three techniques based on the space available to the browser and the minimum size required by the content:

1. Let Grid and Flexbox distribute the remaining space.
2. Use `minmax()`, `min()`, `max()`, and `clamp()` to constrain the allowed range.
3. Change the layout structure when the content can no longer remain usable.

Choose breakpoints where actual content begins to overlap or clip, not from a list of device names or screen resolutions.

References:

- [MDN Responsive web design](https://developer.mozilla.org/en-US/docs/Learn_web_development/Core/CSS_layout/Responsive_Design)
- [MDN CSS length values](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Values/length)
- [MDN CSS container queries](https://developer.mozilla.org/en-US/docs/Web/CSS/Guides/Containment/Container_queries)
- [web.dev Container queries](https://web.dev/learn/css/container-queries/)

## Unit Responsibilities

Choose units according to their purpose instead of standardizing on a single unit.

| Target | Default choice | Rationale |
| --- | --- | --- |
| Available page and panel width | `width: 100%` and `max-width` | Fill smaller screens without stretching excessively on larger screens. |
| Grid columns and rows | `fr`, `minmax()` | Distribute the remaining space with explicit proportions and minimum sizes. |
| Fluid sizing | `clamp()` | Allow a size to change only between defined minimum and maximum values. |
| Typography and general spacing | `rem`, `em`, and unitless `line-height` | Better accommodate user font settings and text scaling. |
| Buttons and inputs | Intrinsic content size, `padding`, and `min-inline-size` | Avoid forcing dynamic labels into fixed widths. |
| Data table columns | `%`, `fr`, and an explicit `table-layout` | Allocate space according to each column's meaning within the same table. |
| Borders and precise dividers | `1px` | Keep visual boundaries consistent. |
| Full-screen height | `min-height: 100dvh` | Accommodate mobile browser chrome changes while allowing content to scroll. |

Fixed `px` values are not prohibited.

They are appropriate when a fixed size is part of the component contract, such as for icons, borders, minimum hit targets, and stable data table columns. Do not, however, use only `px` to fix typography, general spacing, buttons with dynamic labels, or the full width of reusable panels.

## Macro and Component Responsiveness

### Viewport media queries

Use `@media` to change the overall page structure.

- Switch the app bar between horizontal and vertical layouts.
- Switch the full page between multi-column and single-column layouts.
- Change the mobile table presentation.
- Adjust the density of the overall workspace based on viewport height.

Express breakpoints in `em` or `rem` where practical so that the structure also changes when users enlarge text. Migrate existing `px` breakpoints incrementally after adding regression checks at their boundary widths.

### Container queries

Use `@container` when the space available to a component depends on which panel contains it.

- Cleanup operation forms
- Rows containing status text and multiple action buttons
- Summary areas inside modals
- Reusable filter and toolbar groups

Do not preserve a desktop layout merely because the viewport is wide when the component itself is inside a narrow panel.

Declare the container on a stable owning element one level above the component that must respond. A size query then changes the layout of that container's descendants.

```css
.cleanup-workbench-panel {
  container: cleanup-workbench / inline-size;
}

.cleanup-action-row {
  grid-template-columns: minmax(0, 1fr) max-content max-content;
}

@container cleanup-workbench (inline-size < 44rem) {
  .cleanup-retention-form {
    grid-template-columns: minmax(0, 1fr);
  }
}
```

This example illustrates the intended direction. It is not an implementation specification to copy directly into the current stylesheet.

## Component Rules

### Buttons and dynamic labels

- Validate button labels against every possible string, including idle, in-progress, and delete-all states.
- When a label must remain on one line, combine `white-space: nowrap` with sufficient intrinsic width.
- Prefer `max-content` or `min-inline-size` unless a fixed width is part of the product contract.
- When space is insufficient, wrap the button group into another row before arbitrarily shortening labels.
- Keep component dimensions stable in the disabled state.

### Grid and Flexbox

- Prefer Grid for pages and multi-column panels.
- Flexbox is appropriate for one-dimensional button groups and inline controls.
- Use `minmax(0, 1fr)` so long content does not expand a Grid track's implicit minimum width.
- Check whether the sum of content minimum widths, fixed button widths, and gaps can exceed the parent width.
- Do not conceal layout defects with `overflow: hidden`.

### Typography and spacing

- Preserve the current monospace and tabular-number policy for numerical data.
- Prefer relative units for body and control fonts so they respond to user scaling.
- Do not preserve a layout on narrow screens by shrinking text excessively.
- Use unitless line-height ratios where practical.

### Tables

- Percentage widths are acceptable when column meanings are stable and the total allocation is explicit.
- Do not compress every column until the table becomes unreadable on narrow screens.
- Choose one of column omission, row-to-card conversion, or explicit horizontal scrolling.
- Limit horizontal scrolling to areas where users expect it, such as tables and preset strips.

### Modals and viewport height

- Constrain modal width with a form such as `min(<content-max>, calc(100vw - <safe-gap>))`.
- Combine a `100dvh`-based maximum height with internal scrolling.
- Do not use a fixed `height: 100vh` for primary page content.
- Verify height queries for short desktop viewports separately to ensure they do not hide content or overlap actions.

## Current Dashboard Baseline

Patterns to preserve in the current implementation:

- Fluid width and maximum-width constraints on `main`
- Combinations of `fr`, `minmax()`, and `clamp()` in primary columns
- The policy of switching the full layout to a single column at `720px` and below
- Explicit column proportions in data tables
- Viewport-safe gaps and a `100dvh` maximum height for mobile modals

Patterns to improve incrementally:

- Fixed `px` values concentrated in general typography and spacing
- Complex components that respond only to viewport breakpoints
- Fixed-width buttons with dynamic labels
- Structures that hide internal Grid tracks wider than their parent with `overflow`
- One-pixel boundary defects that appear only immediately before or after a breakpoint

## Known Cleanup Constraint

Browser measurements taken on 2026-08-27 identified the following intermediate-width issues in the Cleanup form:

- From viewport widths of `721px` through `757px`, the `All Logs` preset partially overlaps the action row.
- From viewport widths of `721px` through `735px`, the action row intercepts clicks at the center of the `All Logs` button.
- From viewport widths of `721px` through `1101px`, the right edge of the Delete button extends beyond the form area.
- At `720px` and below, the mobile layout takes effect and restores correct behavior.

The `white-space: nowrap` declaration on `Delete All Logs` prevents only the label from wrapping. The parent Grid still requires a separate fix when the sum of its minimum track sizes exceeds the available width.

Treat this constraint as a priority regression target when migrating the Cleanup form to a container-based layout.

## Verification Contract

Do not consider a responsive change complete based only on static CSS string checks.

Minimum verification requirements:

1. `make compile && make test`
2. `make ui-check`
3. Viewports immediately below, exactly at, and immediately above each changed breakpoint
4. Representative viewports at `390px`, `720px`, `1024px`, `1280px`, and `1440px`
5. The longest state for every dynamic label
6. Light and dark themes
7. Browser zoom or text scaling
8. Checks for horizontal overflow, element overlap, and pointer interception

For responsive Cleanup changes, also verify the `721px`, `735px`, `736px`, `757px`, `758px`, `1101px`, and `1102px` boundaries.

Do not execute destructive Cleanup actions during UI layout checks. Select presets and measure DOM geometry only.

## Review Checklist

- Does each component respond to its actual available width?
- Do dynamic strings avoid overlap, clipping, and unwanted wrapping?
- Can the sum of fixed widths and gaps exceed the parent's minimum width?
- Does each breakpoint match an actual content failure point?
- Is `overflow: hidden` concealing a defect?
- Are keyboard and pointer hit targets unobstructed by other elements?
- Is unnecessary horizontal scrolling absent outside tables and similar bounded regions?
- Is information priority preserved on narrow screens?
- Do essential actions remain usable after zooming?
