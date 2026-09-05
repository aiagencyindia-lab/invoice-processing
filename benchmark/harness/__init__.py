"""Benchmark harness: runs the synthetic dataset through the real pipeline.

Lives outside `invoice_processing` on purpose -- it adds no routes, no ports,
no settings, and must never be imported by the product.
"""
