import type React from 'react';
import { BrowserRouter as Router, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import BacktestCenterPage from './pages/BacktestCenterPage';
import SettingsPage from './pages/SettingsPage';
import LoginPage from './pages/LoginPage';
import NotFoundPage from './pages/NotFoundPage';
import PortfolioPage from './pages/PortfolioPage';
import AgentTracePage from './pages/AgentTracePage';
import SignalPreviewPage from './pages/SignalPreviewPage';
import AgentEntryExecutionBacktestsPage from './pages/AgentEntryExecutionBacktestsPage';
import CandidatePoolPage from './pages/CandidatePoolPage';
import SeedPoolQualityPage from './pages/SeedPoolQualityPage';
import NewsSignalsPage from './pages/NewsSignalsPage';
import KronosPage from './pages/KronosPage';
import { ApiErrorAlert, Shell } from './components/common';
import { AuthProvider, useAuth } from './contexts/AuthContext';
import './App.css';

const AppContent: React.FC = () => {
  const location = useLocation();
  const { authEnabled, loggedIn, isLoading, loadError, refreshStatus } = useAuth();

  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-base">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-cyan/20 border-t-cyan" />
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-4 bg-base px-4">
        <div className="w-full max-w-lg">
          <ApiErrorAlert error={loadError} />
        </div>
        <button
          type="button"
          className="btn-primary"
          onClick={() => void refreshStatus()}
        >
          重试
        </button>
      </div>
    );
  }

  if (authEnabled && !loggedIn) {
    if (location.pathname === '/login') {
      return <LoginPage />;
    }
    const redirect = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/login?redirect=${redirect}`} replace />;
  }

  if (location.pathname === '/login') {
    return <Navigate to="/" replace />;
  }

  return (
    <Routes>
      <Route element={<Shell />}>
        <Route path="/" element={<Navigate to="/agent-trace" replace />} />
        <Route path="/agent-trace" element={<AgentTracePage />} />
        <Route path="/agent-trace/:sessionId" element={<AgentTracePage />} />
        <Route path="/signal-preview" element={<SignalPreviewPage />} />
        <Route path="/agent-verdict-reviews" element={<Navigate to="/signal-preview" replace />} />
        <Route path="/agent-verdict-reviewsz" element={<Navigate to="/signal-preview" replace />} />
        <Route path="/agent-entry-execution-backtests" element={<AgentEntryExecutionBacktestsPage />} />
        <Route path="/candidate-pool" element={<CandidatePoolPage />} />
        <Route path="/seed-pool-quality" element={<SeedPoolQualityPage />} />
        <Route path="/news-signals" element={<NewsSignalsPage />} />
        <Route path="/kronos" element={<KronosPage />} />
        <Route path="/portfolio" element={<PortfolioPage />} />
        <Route path="/backtest" element={<BacktestCenterPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
      <Route path="/login" element={<LoginPage />} />
    </Routes>
  );
};

const App: React.FC = () => {
  return (
    <Router>
      <AuthProvider>
        <AppContent />
      </AuthProvider>
    </Router>
  );
};

export default App;
