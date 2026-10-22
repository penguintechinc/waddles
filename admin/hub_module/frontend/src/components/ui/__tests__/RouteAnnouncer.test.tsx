import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom';
import { RouteAnnouncer } from '../RouteAnnouncer';
import { MAIN_CONTENT_ID } from '../constants';

function App() {
  return (
    <MemoryRouter initialEntries={['/a']}>
      <RouteAnnouncer />
      <Link to="/b">go b</Link>
      <Link to="/c">go c</Link>
      <main id={MAIN_CONTENT_ID} tabIndex={-1}>
        <Routes>
          <Route path="/a" element={<h1>Alpha</h1>} />
          <Route path="/b" element={<h1>Bravo</h1>} />
          <Route path="/c" element={<p>no heading</p>} />
        </Routes>
      </main>
    </MemoryRouter>
  );
}

describe('RouteAnnouncer', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.title = 'Waddles Hub';
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('does nothing on the initial render', () => {
    render(<App />);
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(screen.getByTestId('route-announcer')).toHaveTextContent('');
    expect(document.title).toBe('Waddles Hub');
  });

  it('announces, retitles and focuses main after navigation', () => {
    render(<App />);
    act(() => {
      screen.getByText('go b').click();
    });
    act(() => {
      vi.advanceTimersByTime(200);
    });
    expect(screen.getByTestId('route-announcer')).toHaveTextContent('Navigated to Bravo');
    expect(document.title).toBe('Bravo · Waddles');
    expect(document.getElementById(MAIN_CONTENT_ID)).toHaveFocus();
  });

  it('falls back to a generic title when the page never renders an h1', () => {
    render(<App />);
    act(() => {
      screen.getByText('go c').click();
    });
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(screen.getByTestId('route-announcer')).toHaveTextContent('Navigated to New page');
  });
});
