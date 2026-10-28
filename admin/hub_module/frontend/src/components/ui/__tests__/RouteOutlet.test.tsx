import { lazy, type ReactElement } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom';
import { RouteOutlet } from '../RouteOutlet';
import { RouteErrorBoundary } from '../RouteErrorBoundary';

function Boom(): never {
  throw new Error('render failed');
}

const Slow = lazy(() => new Promise<{ default: () => ReactElement }>((resolve) => {
  setTimeout(() => resolve({ default: () => <h1>Loaded page</h1> }), 0);
}));

function Shell({ initial }: { initial: string }) {
  return (
    <MemoryRouter initialEntries={[initial]}>
      <Link to="/ok">go ok</Link>
      <Routes>
        <Route element={<RouteOutlet />}>
          <Route path="/boom" element={<Boom />} />
          <Route path="/ok" element={<h1>Fine page</h1>} />
          <Route path="/slow" element={<Slow />} />
        </Route>
      </Routes>
    </MemoryRouter>
  );
}

describe('RouteOutlet', () => {
  it('shows a loading state inside the layout while a lazy route resolves', async () => {
    render(<Shell initial="/slow" />);
    expect(screen.getByTestId('loading-state')).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: 'Loaded page' })).toBeInTheDocument();
  });

  it('contains a throwing route and recovers when the user navigates away', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    render(<Shell initial="/boom" />);
    expect(screen.getByRole('alert')).toHaveTextContent(/hit a problem/i);
    act(() => {
      fireEvent.click(screen.getByText('go ok'));
    });
    expect(screen.getByRole('heading', { name: 'Fine page' })).toBeInTheDocument();
    spy.mockRestore();
  });
});

describe('RouteErrorBoundary actions', () => {
  it('reloads the app from the fallback and clears the error via "Try again"', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const reload = vi.fn();
    vi.stubGlobal('location', { ...window.location, reload });
    let shouldThrow = true;
    function Flaky() {
      if (shouldThrow) throw new Error('render failed');
      return <p>recovered</p>;
    }
    render(
      <RouteErrorBoundary>
        <Flaky />
      </RouteErrorBoundary>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Reload the app' }));
    expect(reload).toHaveBeenCalledTimes(1);
    shouldThrow = false;
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(screen.getByText('recovered')).toBeInTheDocument();
    vi.unstubAllGlobals();
    spy.mockRestore();
  });
});
