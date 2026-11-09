import { useQuery } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import { getReportsEnabled } from '@/api/reports';
import { isPlatformMode } from '@/config/hostMode';

/** Whether this deployment takes reports, ``undefined`` while the answer is on
 *  its way. A self-hosted build has nowhere to send them, so it never asks,
 *  and a failed read hides the entry points. */
export function useReportsState(): boolean | undefined {
  const { data, isError } = useQuery({
    queryKey: queryKeys.reports.config,
    queryFn: getReportsEnabled,
    enabled: isPlatformMode,
    staleTime: Infinity,
  });
  if (!isPlatformMode || isError) return false;
  return data;
}

export function useReportsEnabled(): boolean {
  return useReportsState() ?? false;
}
