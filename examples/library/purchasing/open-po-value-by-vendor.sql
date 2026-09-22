-- name: Open PO value by vendor
-- description: Open purchase-order value per vendor for orders placed on or after a start date.
--    Aggregates lines first so the header-to-line fan-out cannot inflate anything.
-- connection: erp
-- tags: purchasing, monthly
-- param: @StartDate date = '2026-01-01' | first order date to include
-- param: @MinValue decimal(18,2) = 0 | hide vendors below this open value

WITH line_totals AS (
    SELECT l.PoId, SUM(l.Amount) AS PoValue
    FROM dbo.PoLine AS l
    GROUP BY l.PoId
)
SELECT v.VendorId, v.Name, COUNT(*) AS OpenPos, SUM(t.PoValue) AS OpenValue
FROM dbo.PoHeader AS h
JOIN dbo.Vendor AS v ON v.VendorId = h.VendorId
JOIN line_totals AS t ON t.PoId = h.PoId
WHERE h.Status = 'OPEN'
  AND h.OrderDate >= @StartDate
GROUP BY v.VendorId, v.Name
HAVING SUM(t.PoValue) >= @MinValue
ORDER BY OpenValue DESC
