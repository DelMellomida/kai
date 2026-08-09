"""Kai's dashboard — the only operator interface the robot has.

  state.py     what the dashboard is currently being told, behind one lock
  server.py    the Flask app: every route, built around injected collaborators
  frontend/    dashboard.html and guide.html, served as static files

Nothing here reaches for robot state on its own. face_track.py constructs the voice assistant, the
conversation session and the camera supervisor and hands them to Dashboard, so this package can be
imported — and its routes exercised — with none of that hardware present.
"""
