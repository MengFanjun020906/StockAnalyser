import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { SidebarNav } from '../SidebarNav';

const mockLogout = vi.fn().mockResolvedValue(undefined);
const mockThemeToggle = vi.fn(({ collapsed }: { collapsed?: boolean }) => (
  <button type="button">{collapsed ? '切换主题(折叠)' : '切换主题'}</button>
));

vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({
    authEnabled: true,
    logout: mockLogout,
  }),
}));

vi.mock('../../theme/ThemeToggle', () => ({
  ThemeToggle: (props: { collapsed?: boolean }) => mockThemeToggle(props),
}));

describe('SidebarNav', () => {
  it('does not render the removed home, chat, or Jev entries', () => {
    render(
      <MemoryRouter initialEntries={['/agent-trace']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    expect(screen.queryByRole('link', { name: '首页' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: '问股' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Jev' })).not.toBeInTheDocument();
  });

  it('renders the collapsed theme toggle variant when the sidebar is collapsed', () => {
    render(
      <MemoryRouter initialEntries={['/agent-trace']}>
        <SidebarNav collapsed />
      </MemoryRouter>,
    );

    expect(mockThemeToggle).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'nav', collapsed: true }),
    );
    expect(screen.getByRole('button', { name: '切换主题(折叠)' })).toBeInTheDocument();
  });

  it('renders the developer trace navigation item', () => {
    render(
      <MemoryRouter initialEntries={['/agent-trace']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    expect(screen.getByRole('link', { name: '链路' })).toHaveAttribute('href', '/agent-trace');
  });

  it('renders the Kronos K-line navigation item', () => {
    render(
      <MemoryRouter initialEntries={['/agent-trace']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    expect(screen.getByRole('link', { name: 'K线' })).toHaveAttribute('href', '/kronos');
  });

  it('links the signal navigation item to the preview page', () => {
    render(
      <MemoryRouter initialEntries={['/agent-trace']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    expect(screen.getByRole('link', { name: '信号' })).toHaveAttribute('href', '/signal-preview');
    expect(screen.queryByRole('link', { name: '复盘' })).not.toBeInTheDocument();
  });

  it('replaces the candidate-pool navigation item with entry backtests', () => {
    render(
      <MemoryRouter initialEntries={['/agent-trace']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    expect(screen.getByRole('link', { name: '入场' })).toHaveAttribute('href', '/agent-entry-execution-backtests');
    expect(screen.queryByRole('link', { name: '候选池' })).not.toBeInTheDocument();
  });

  it('opens the logout confirmation and confirms logout', async () => {
    render(
      <MemoryRouter initialEntries={['/agent-trace']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole('button', { name: '退出' }));

    expect(await screen.findByRole('heading', { name: '退出登录' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '确认退出' }));
    expect(mockLogout).toHaveBeenCalled();
  });
});
