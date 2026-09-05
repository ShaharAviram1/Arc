import { createBrowserRouter } from 'react-router-dom'
import { Layout } from '@/components/Layout'
import { RouteError } from '@/components/RouteError'
import { Admin } from '@/pages/Admin'
import { Home } from '@/pages/Home'
import { Invite } from '@/pages/Invite'
import { Login } from '@/pages/Login'
import { Mal } from '@/pages/Mal'
import { NotFound } from '@/pages/NotFound'
import { Player } from '@/pages/Player'
import { Recs } from '@/pages/Recs'
import { Review } from '@/pages/Review'
import { Schedule } from '@/pages/Schedule'
import { Search } from '@/pages/Search'
import { Show } from '@/pages/Show'

export const routes = [
  // Auth pages render without the sidebar chrome.
  { path: '/login', element: <Login /> },
  { path: '/invite/:token', element: <Invite /> },
  {
    element: <Layout />,
    errorElement: <RouteError />,
    children: [
      { path: '/', element: <Home /> },
      { path: '/schedule', element: <Schedule /> },
      { path: '/search', element: <Search /> },
      { path: '/anime/:id', element: <Show /> },
      { path: '/watch/:episodeId', element: <Player /> },
      { path: '/mal', element: <Mal /> },
      { path: '/recs', element: <Recs /> },
      { path: '/review', element: <Review /> },
      { path: '/admin', element: <Admin /> },
      { path: '*', element: <NotFound /> },
    ],
  },
]

export const router = createBrowserRouter(routes)
