import { useCallback, useEffect, useState } from 'react';
import { analyticsApi } from '../../services/api';
import { classifyAnalyticsError } from './classifyAnalyticsError';

const IDLE = { loading: true, data: null, failure: null };

const fetchers = (period) => ({
  overview: () => analyticsApi.getPlatformOverview(),
  reputation: () => analyticsApi.getPlatformReputation(),
  growth: () => analyticsApi.getPlatformGrowth(period),
  activity: () => analyticsApi.getPlatformActivity(),
  health: () => analyticsApi.getCommunityHealth(),
});

/**
 * Loads the five platform-analytics widgets independently (`Promise.allSettled`), so one failed
 * call only affects its own widget. Returns per-widget `{loading, data, failure}`, `retry(key)`
 * and the growth period control.
 */
export function usePlatformAnalytics(initialPeriod) {
  const [period, setPeriod] = useState(initialPeriod);
  const [widgets, setWidgets] = useState({
    overview: IDLE,
    reputation: IDLE,
    growth: IDLE,
    activity: IDLE,
    health: IDLE,
  });

  const load = useCallback(async (key, fetcher) => {
    setWidgets((w) => ({ ...w, [key]: IDLE }));
    try {
      const res = await fetcher();
      setWidgets((w) => ({ ...w, [key]: { loading: false, data: res.data ?? null, failure: null } }));
    } catch (err) {
      setWidgets((w) => ({ ...w, [key]: { loading: false, data: null, failure: classifyAnalyticsError(err) } }));
    }
  }, []);

  useEffect(() => {
    // `load` never rejects; allSettled keeps the five widgets independent.
    Promise.allSettled(Object.entries(fetchers(initialPeriod)).map(([key, fn]) => load(key, fn)));
  }, [initialPeriod, load]);

  const changePeriod = useCallback(
    (next) => {
      setPeriod(next);
      load('growth', fetchers(next).growth);
    },
    [load],
  );

  const retry = useCallback((key) => load(key, fetchers(period)[key]), [load, period]);

  return { widgets, period, changePeriod, retry };
}

/** True when every value is null/undefined/0 (an absent or all-zero counter set). */
export function allZero(values) {
  return values.every((v) => v === null || v === undefined || Number(v) === 0);
}
