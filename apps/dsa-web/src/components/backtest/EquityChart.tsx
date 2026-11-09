import { useEffect, useRef } from 'react';
import * as echarts from 'echarts';
import type { BenchmarkSeries, EquityPoint } from '../../api/backtestRuns';

type EquityChartProps = {
  points: EquityPoint[];
  benchmarks?: BenchmarkSeries[];
  height?: number;
};

const BENCHMARK_COLORS = ['#a78bfa', '#f59e0b', '#34d399', '#f472b6'];

export const EquityChart = ({ points, benchmarks = [], height = 320 }: EquityChartProps) => {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = echarts.init(containerRef.current);
    chartRef.current = chart;
    const handleResize = () => chart.resize();
    window.addEventListener('resize', handleResize);
    return () => {
      window.removeEventListener('resize', handleResize);
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    if (!points.length) {
      chart.clear();
      return;
    }
    const hasBenchmark = points.some((point) => point.benchmarkValue != null);
    const sessions = points.map((point) => point.session);
    const series: echarts.EChartsOption['series'] = [
      {
        name: '策略权益',
        type: 'line',
        data: points.map((point) => point.equity),
        showSymbol: false,
        smooth: false,
        lineStyle: { color: '#22d3ee', width: 2 },
        areaStyle: { color: 'rgba(34, 211, 238, 0.10)' },
      },
    ];
    if (hasBenchmark) {
      series.push({
        name: '基准',
        type: 'line',
        data: points.map((point) => point.benchmarkValue ?? null),
        showSymbol: false,
        smooth: false,
        lineStyle: { color: '#a78bfa', width: 1.5, type: 'dashed' },
      });
    }
    benchmarks.forEach((benchmark, index) => {
      if (!benchmark.points.length) return;
      const bySession = new Map(benchmark.points.map((point) => [point.session, point.value]));
      series.push({
        name: benchmark.name,
        type: 'line',
        data: sessions.map((session) => bySession.get(session) ?? null),
        showSymbol: false,
        smooth: false,
        connectNulls: true,
        lineStyle: {
          color: BENCHMARK_COLORS[index % BENCHMARK_COLORS.length],
          width: 1.3,
          type: 'dashed',
        },
      });
    });
    series.push({
      name: '回撤 %',
      type: 'line',
      yAxisIndex: 1,
      data: points.map((point) => point.drawdownPct),
      showSymbol: false,
      lineStyle: { color: '#fb7185', width: 1 },
      areaStyle: { color: 'rgba(251, 113, 133, 0.06)' },
    });
    const option: echarts.EChartsOption = {
      animation: false,
      backgroundColor: 'transparent',
      tooltip: { trigger: 'axis', valueFormatter: (value) => (typeof value === 'number' ? value.toFixed(2) : String(value)) },
      legend: { top: 0, textStyle: { color: '#94a3b8' } },
      grid: { left: 56, right: 56, top: 36, bottom: 28 },
      xAxis: {
        type: 'category',
        boundaryGap: false,
        data: points.map((point) => point.session),
        axisLabel: { color: '#64748b' },
        axisLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.2)' } },
      },
      yAxis: [
        {
          type: 'value',
          scale: true,
          axisLabel: { color: '#64748b' },
          splitLine: { lineStyle: { color: 'rgba(148, 163, 184, 0.10)' } },
        },
        {
          type: 'value',
          max: 0,
          axisLabel: { color: '#fb7185', formatter: '{value}%' },
          splitLine: { show: false },
        },
      ],
      series,
    };
    chart.setOption(option, true);
  }, [points, benchmarks]);

  return <div ref={containerRef} style={{ height }} className="w-full" />;
};
