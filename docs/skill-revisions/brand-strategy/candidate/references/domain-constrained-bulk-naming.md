# Domain-kısıtlı toplu marka adı taraması

## Tetikleyici

Kullanıcı 40–50 gibi geniş bir aday havuzu ve gerçekten kayıtsız exact `.com` isterse kullan. Bu, normal 8–12 aday / finalist-sonrası domain taraması akışının bilinçli istisnasıdır.

## Ön karar kapısı

İsim üretmeden önce exact domain kuralını kesinleştir:

- exact `<marka>.com`
- ekli `.com` (`playX.com`, `Xgames.com`, `Xoss.com`)
- kategori TLD'si (`.games`, `.dev`, `.org`)

“Domain boş olsun” ifadesinden TLD veya prefix politikasını tahmin etme. Exact tek kelime İngilizce `.com` ile 40–50 seçenek hedefi çoğu zaman gerçek sözlük kelimeleriyle bağdaşmaz; kullanıcı exact `.com` seçerse semantik türetme veya yeni kelime üretme trade-off'unu açıkla.

## Doğrulanmış toplu yöntem

1. Her marka hattı için anlam kökleri oluştur; bağımsız markaları tek havuza karıştırma.
2. Kullanıcının reddettiği biçimleri (mistik heceler, jenerik birleşikler, yanlış dil) üretimden çıkar.
3. İstenen teslim sayısından birkaç kat büyük iç havuz üret.
4. Her exact domain için `references/domain-constrained-brand-longlists.md` içindeki Registration-evidence procedure yöntemini uygula.
5. Belirsiz veya çelişkili sonucu “boş” diye gösterme.
6. Exact domain, kaynak, kontrol zamanı ve sınırlı sonucu kaydet: “kontrol anında kayıt nesnesi bulunmadı”. Satın alınabilirlik ve hukuki uygunluk ayrı kalır.
7. Domain filtresinden geçen uzun listeyi sunduktan sonra kullanıcı favorileri için exact web, şirket, oyun/ürün, GitHub, paket, sosyal yüzey ve marka veritabanı taramasını yap.

## Performans

- Binlerce RDAP isteğini tek foreground turda çalıştırma; bounded concurrency ve küçük batch kullan.
- Küçük temsilî havuzla uygulanabilirliği ölç; batch boyutunu istenen kapsam ve servis limitlerine göre belirle.
- Timeout olan batch'i “kayıtsız” sayma. Yanıtın doğru registry ve exact sorguya ait olduğunu doğrula.
- WHOIS kullanılıyorsa destek ve limitlerini doğrula; sabit worker sayısını zorunlu tutma.

## Pitfall'lar

- DNS NXDOMAIN tek başına sahiplik kanıtı değildir.
- RDAP 404 tek başına satın alınabilirlik garantisi değildir; kanonik kanıt yöntemindeki belirsizlik sınırlarını koru.
- Registrar sepeti/ödeme testi harcama ve kullanıcı onayı gerektirir; availability araştırması bunu otomatik satın almaya çeviremez.
- Domain bulmak için kullanıcının reddettiği isim estetiğine sessizce geri dönme. Exact `.com` ile sözlük kelimesi hedefi çatışıyorsa trade-off'u göster.
- 50 ham adı, sektör ve marka çakışması temizlenmiş 50 finalist gibi sunma. “Domain-doğrulanmış uzun liste” ile “marka finalistleri” ayrı aşamalardır.
