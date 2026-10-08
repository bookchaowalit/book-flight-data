---
title: Book Flight Data — Product Card
doc_id: CARD-book-flight-data
doc_type: product-card
verdict: showcase
version: 0.1
date: 2026-10-08
status: ร่างเพื่อพิจารณา
owner: Product owner (Book Dev)
---

# Book Flight Data — Product Card

## 1. สรุปคุณค่าในหนึ่งประโยค

รวบรวมราคาตั๋วเครื่องบินจากหลายแหล่ง เก็บแต่ละ response แยกก่อน merge เพื่อให้ตรวจย้อนได้ สำหรับการเฝ้าดูราคาส่วนตัว

## 2. ปัญหาและผู้ใช้เป้าหมาย

- การเฝ้าดูราคาตั๋วด้วยมือเสียเวลา และผลจากหลายแหล่งเทียบกันยาก
- ผู้ใช้: เจ้าของและระบบภายใน; ถูกใช้โดย `book-opportunity-intelligence` (synthesis) และ `solo-empire-insights` (หน้าเว็บ)

## 3. สิ่งที่ทำได้จริงตอนนี้

- `ingest.py` (ใหญ่สุดในกลุ่ม ~800 บรรทัด) รองรับ Kiwi Tequila API, Skyscanner HTML และ Firecrawl; landing แยกตาม provider
- `api.py` อ่านผ่าน DuckDB (port 8105) datasets `flight_prices`, `flight_prices_history`
- test 12 ไฟล์

## 4. หลักฐานสถานะ

- Stack: Python
- Commits: 15 (แรก 2026-08-11 → ล่าสุด 2026-10-01), branch `main`; working tree สะอาด
- Source: 19 ไฟล์ / 3,211 บรรทัด; test 12 ไฟล์
- CI: 1 workflow
- Deploy config: ไม่มี
- Live URL: ไม่พบใน repo
- โครงสร้างไฟล์เหมือนกับ repo อื่นอีก 2 ตัวในตระกูลเดียวกัน (structure signature เดียวกัน)

## 5. คุณค่าและจุดต่าง

- แสดงการจัดการหลาย provider และ HTML landing ที่เก็บ bytes ดิบ
- บริการค้นตั๋ว (Google Flights, Skyscanner) ดีกว่ามากสำหรับผู้ใช้ทั่วไป; ไม่มีมุมรายได้

## 6. ช่องว่างสำคัญ

- ไม่มี deploy config หรือ public endpoint; ใช้งานผ่าน API ในเครื่องเท่านั้น (ค่าเริ่มต้น bind 127.0.0.1)
- การ scrape HTML ของ Skyscanner เสี่ยงต่อเงื่อนไขการใช้งาน **[ต้องยืนยัน]**
- Kiwi/Firecrawl ต้องใช้ key จาก environment

## 7. คำตัดสิน

**showcase** — เป็นตัวอย่าง multi-provider ingest ที่ดี แต่ไม่ใช่ธุรกิจและมีความเสี่ยงด้านเงื่อนไขแหล่งข้อมูล

จะเปลี่ยนเป็น `archive` ถ้าแหล่งที่อนุญาตไม่มีเหลือ หรือ `merge` ถ้ารวม repo ข้อมูลตลาด

## 8. ขั้นต่อไป

1. ทบทวนเงื่อนไขการใช้ Skyscanner และปิด provider ที่ไม่อนุญาต
2. ตัดสินใจรวม 6 repo ข้อมูลตลาด

## การอนุมัติ

| บทบาท | สถานะ | วันที่ | ลงนาม |
|---|---|---|---|
| Product owner (Book Dev) | รอพิจารณา | — | ________ |

## แหล่งข้อมูล

- `README.md`
- `src/book_flight/ingest.py`
- `src/book_flight/api.py`
- `tests/`
