"""The decision engine: capture, project, recommend, and grade.

Reads the FPL API and its own warehouse; knows nothing about MCP. The dependency runs
one way - storage is a leaf, projection builds on it, recommend builds on projection -
so the engine can be used, tested and scheduled without the server.
"""
