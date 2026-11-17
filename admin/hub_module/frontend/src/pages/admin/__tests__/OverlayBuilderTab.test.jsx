import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

import api from '../../../services/api';
import { apiClient } from '../../../lib/apiClient';
import { stubHttp } from '../../../test/httpStub';
import OverlayBuilderTab from '../OverlayBuilderTab';

const BASE = '/api/v1/admin/7/overlay';

const widget = (over = {}) => ({
  widget_id: 'alerts.event_box',
  bundle_app_id: 'bundle.alerts',
  display_name: 'Alert Box',
  description: 'Sub and follow alerts',
  category: 'alerts',
  output_kind: 'sanitized_html',
  surface_id: null,
  allow_standalone_output: true,
  default_x: 660,
  default_y: 120,
  default_width: 600,
  default_height: 220,
  default_z_index: 10,
  props_schema: {},
  ...over,
});

const component = (over = {}) => ({
  instance_id: 'c0ffee00-0000-4000-8000-000000000001',
  bundle_app_id: 'bundle.alerts',
  widget_id: 'alerts.event_box',
  surface_id: 'custom',
  output_kind: 'sanitized_html',
  display_name: 'Alert Box',
  x: 660,
  y: 120,
  width: 600,
  height: 220,
  z_index: 10,
  visible: true,
  props: {},
  ...over,
});

const httpError = (status, message) =>
  Object.assign(new Error(message), {
    isAxiosError: true,
    response: { status, data: { message }, statusText: '', headers: {} },
  });

/** Selecting happens on mousedown (it also starts a drag); release without moving. */
function selectComponent(el) {
  fireEvent.mouseDown(el, { clientX: 10, clientY: 10 });
  fireEvent.mouseUp(window);
}

let http;
afterEach(() => {
  http?.restore();
  vi.restoreAllMocks();
});

function routes(over = {}) {
  return {
    [`GET ${BASE}/layout`]: {
      success: true,
      layout: { canvas: { resolution: '1080p', width: 1920, height: 1080 }, components: [] },
    },
    [`GET ${BASE}/widgets`]: { success: true, widgets: [widget()] },
    'GET /api/v1/admin/7/browser-sources': {
      success: true,
      sources: [{ sourceType: 'scene', url: 'https://overlay.test/code/scene?key=k', isActive: true }],
    },
    ...over,
  };
}

describe('OverlayBuilderTab', () => {
  it('lists the palette keyed on bundle + widget so same-named widgets both show', async () => {
    http = stubHttp(
      [api, apiClient],
      routes({
        [`GET ${BASE}/widgets`]: {
          success: true,
          widgets: [
            widget(),
            widget({ bundle_app_id: 'bundle.other', display_name: 'Other Alert Box' }),
          ],
        },
      }),
    );
    render(<OverlayBuilderTab communityId="7" />);
    expect(await screen.findByText('Alert Box')).toBeTruthy();
    expect(screen.getByText('Other Alert Box')).toBeTruthy();
    expect(screen.getByText('bundle.other')).toBeTruthy();
    expect(screen.getByText('Preview').closest('a').getAttribute('href')).toBe(
      'https://overlay.test/code/scene?key=k',
    );
  });

  it('adds a widget sending both halves of its identity and shows it selected', async () => {
    http = stubHttp(
      [api, apiClient],
      routes({ [`POST ${BASE}/layout/components`]: () => ({ success: true, component: component() }) }),
    );
    render(<OverlayBuilderTab communityId="7" />);
    fireEvent.click(await screen.findByText('Add to Canvas'));
    expect(await screen.findByTestId(`component-${component().instance_id}`)).toBeTruthy();
    expect(http.seen.find((r) => r.method === 'POST').body).toEqual({
      bundle_app_id: 'bundle.alerts',
      widget_id: 'alerts.event_box',
    });
    expect(screen.getByText('1 component placed')).toBeTruthy();
    expect(screen.getByText(/Component:/)).toBeTruthy();
  });

  it('commits an inspector edit once on blur, clamped to the canvas', async () => {
    const patches = [];
    http = stubHttp(
      [api, apiClient],
      routes({
        [`GET ${BASE}/layout`]: {
          success: true,
          layout: {
            canvas: { resolution: '1080p', width: 1920, height: 1080 },
            components: [component()],
          },
        },
        [`PATCH ${BASE}/layout/components/${component().instance_id}`]: (req) => {
          patches.push(req.body);
          return { success: true, component: component(req.body) };
        },
      }),
    );
    render(<OverlayBuilderTab communityId="7" />);
    selectComponent(await screen.findByTestId(`component-${component().instance_id}`));
    const x = screen.getByLabelText('X Position');
    fireEvent.change(x, { target: { value: '9999' } });
    expect(patches).toEqual([]);
    fireEvent.blur(x);
    await waitFor(() => expect(patches).toEqual([{ x: 1888 }]));
  });

  it('persists a drag once, on release', async () => {
    const patches = [];
    http = stubHttp(
      [api, apiClient],
      routes({
        [`GET ${BASE}/layout`]: {
          success: true,
          layout: {
            canvas: { resolution: '1080p', width: 1920, height: 1080 },
            components: [component()],
          },
        },
        [`PATCH ${BASE}/layout/components/${component().instance_id}`]: (req) => {
          patches.push(req.body);
          return { success: true, component: component(req.body) };
        },
      }),
    );
    render(<OverlayBuilderTab communityId="7" />);
    const canvas = await screen.findByTestId('builder-canvas');
    canvas.getBoundingClientRect = () => ({ width: 960, height: 540, left: 0, top: 0 });
    const comp = screen.getByTestId(`component-${component().instance_id}`);
    fireEvent.mouseDown(comp, { clientX: 100, clientY: 100 });
    fireEvent.mouseMove(window, { clientX: 150, clientY: 130 });
    fireEvent.mouseMove(window, { clientX: 200, clientY: 160 });
    expect(patches).toEqual([]);
    fireEvent.mouseUp(window);
    await waitFor(() => expect(patches).toEqual([{ x: 860, y: 240 }]));
  });

  it('changes resolution through the canvas endpoint and surfaces a 409 message', async () => {
    let fail = false;
    http = stubHttp(
      [api, apiClient],
      routes({
        [`PUT ${BASE}/layout/canvas`]: (req) => {
          if (fail) throw httpError(409, 'Component "x" does not fit 1080p; move or remove it first');
          return { success: true, canvas: { resolution: req.body.resolution, width: 3840, height: 2160 } };
        },
      }),
    );
    render(<OverlayBuilderTab communityId="7" />);
    fireEvent.click(await screen.findByText('4K'));
    expect(await screen.findByText('Canvas resolution saved')).toBeTruthy();
    expect(screen.getByText(/3840 × 2160/)).toBeTruthy();

    fail = true;
    fireEvent.click(screen.getByText('1080P'));
    expect((await screen.findByRole('alert')).textContent).toContain('does not fit 1080p');
  });

  it('clears the canvas only after confirmation', async () => {
    http = stubHttp(
      [api, apiClient],
      routes({
        [`GET ${BASE}/layout`]: {
          success: true,
          layout: {
            canvas: { resolution: '1080p', width: 1920, height: 1080 },
            components: [component()],
          },
        },
        [`DELETE ${BASE}/layout`]: { success: true, removed_count: 1 },
      }),
    );
    render(<OverlayBuilderTab communityId="7" />);
    await screen.findByTestId(`component-${component().instance_id}`);
    fireEvent.click(screen.getByText('Clear'));
    expect(http.seen.some((r) => r.method === 'DELETE')).toBe(false);
    fireEvent.click(within(screen.getByRole('dialog')).getByText('Clear All'));
    await waitFor(() => expect(screen.queryByTestId(`component-${component().instance_id}`)).toBeNull());
    expect(screen.getByText('Empty Canvas')).toBeTruthy();
  });

  it('tells the admin when the plan does not include the builder', async () => {
    http = stubHttp(
      [api, apiClient],
      routes({
        [`GET ${BASE}/layout`]: () => {
          throw httpError(402, 'nope');
        },
      }),
    );
    render(<OverlayBuilderTab communityId="7" />);
    expect((await screen.findByRole('alert')).textContent).toContain('not enabled for this plan');
  });

  it('removes a component and aligns another via presets', async () => {
    const patches = [];
    http = stubHttp(
      [api, apiClient],
      routes({
        [`GET ${BASE}/layout`]: {
          success: true,
          layout: {
            canvas: { resolution: '1080p', width: 1920, height: 1080 },
            components: [component()],
          },
        },
        [`PATCH ${BASE}/layout/components/${component().instance_id}`]: (req) => {
          patches.push(req.body);
          return { success: true, component: component(req.body) };
        },
        [`DELETE ${BASE}/layout/components/${component().instance_id}`]: { success: true, removed: true },
      }),
    );
    render(<OverlayBuilderTab communityId="7" />);
    selectComponent(await screen.findByTestId(`component-${component().instance_id}`));
    fireEvent.click(screen.getByText('Center'));
    await waitFor(() => expect(patches).toEqual([{ x: 660, y: 430 }]));
    fireEvent.click(screen.getByLabelText('Hide from overlay'));
    await waitFor(() => expect(patches[1]).toEqual({ visible: false }));
    fireEvent.click(screen.getByLabelText('Remove component'));
    await waitFor(() => expect(screen.getByText('Empty Canvas')).toBeTruthy());
  });

  it('moves a component with the arrow keys and selects with Enter', async () => {
    const patches = [];
    http = stubHttp(
      [api, apiClient],
      routes({
        [`GET ${BASE}/layout`]: {
          success: true,
          layout: {
            canvas: { resolution: '1080p', width: 1920, height: 1080 },
            components: [component()],
          },
        },
        [`PATCH ${BASE}/layout/components/${component().instance_id}`]: (req) => {
          patches.push(req.body);
          return { success: true, component: component(req.body) };
        },
      }),
    );
    render(<OverlayBuilderTab communityId="7" />);
    const comp = await screen.findByTestId(`component-${component().instance_id}`);
    fireEvent.keyDown(comp, { key: 'Enter' });
    expect(screen.getByText(/Component:/)).toBeTruthy();
    fireEvent.keyDown(comp, { key: 'ArrowRight' });
    await waitFor(() => expect(patches).toEqual([{ x: 670, y: 120 }]));
    fireEvent.keyDown(comp, { key: 'ArrowUp', shiftKey: true });
    await waitFor(() => expect(patches[1]).toEqual({ x: 670, y: 70 }));
    fireEvent.click(screen.getByLabelText('Deselect component'));
    expect(screen.queryByText(/Component:/)).toBeNull();
  });
});
