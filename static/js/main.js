const navToggle = document.querySelector(".nav-toggle");
const navLinks = document.querySelector(".nav-links");

if (navToggle && navLinks) {
    navToggle.addEventListener("click", () => {
        navLinks.classList.toggle("open");
    });
}

const orderType = document.querySelector("#orderType");
const deliveryAddress = document.querySelector("#deliveryAddress");

function toggleDeliveryAddress() {
    if (!orderType || !deliveryAddress) return;
    deliveryAddress.classList.toggle("hidden", orderType.value !== "delivery");
}

if (orderType) {
    orderType.addEventListener("change", toggleDeliveryAddress);
    toggleDeliveryAddress();
}

setTimeout(() => {
    document.querySelectorAll(".flash").forEach((flash) => {
        flash.style.opacity = "0";
        flash.style.transform = "translateY(-8px)";
        flash.style.transition = "opacity .25s ease, transform .25s ease";
    });
}, 4200);
