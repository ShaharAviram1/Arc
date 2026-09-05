import { createBrowserRouter } from 'react-router-dom'
import { Layout } from '@/components/Layout'
import { RequireAdmin } from '@/components/RequireAdmin'
import { RequireAuth } from '@/components/RequireAuth'
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
  // Auth pages render without the sidebar chrome and without a session. They
  // sit outside the layout, so they need their own error boundary — otherwise
  // a throw here escapes to the router's blank default page.
  { path: '/login', element: <Login />, errorElement: <RouteError /> },
  { path: '/invite/:token', element: <Invite />, errorElement: <RouteError /> },
  {
    // Everything below needs a session (spec §7: all API routes are behind
    // auth, so an unauthenticated app shell would only render errors).
    element: <RequireAuth />,
    children: [
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
          // Review is per-user in phase 1 (spec §2: users resolve review items
          // for their own shows); only /admin is role-gated.
          { path: '/review', element: <Review /> },
          {
            element: <RequireAdmin />,
            children: [{ path: '/admin', element: <Admin /> }],
          },
          { path: '*', element: <NotFound /> },
        ],
      },
    ],
  },
]

export const router = createBrowserRouter(routes)
